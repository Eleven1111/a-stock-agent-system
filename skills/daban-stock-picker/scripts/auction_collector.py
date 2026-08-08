#!/usr/bin/env python3
"""
集合竞价采集器 — 9:15-9:25 真竞价微观结构因子
=================================================
腾讯 qt.gtimg.cn 全天候免费、不受 ClashX TUN 影响，且其报文 parts[9..28] 自带
五档盘口（原 data_sources.md 漏记）。竞价期间这五档反映累积委买/委卖，是免费源
最接近 L2 的竞价信号。

本采集器把单一手填的 `auction_gap_pct` 升级为 6 个可审计的真竞价因子，
输出可直接并入 daban_candidate_api 的候选字段，不替代回测闸门，不自动下单。

工作流（cron 9:15-9:25 每分钟）：
  python auction_collector.py --snapshot   # 自动读取前一交易日动态观察池
  python auction_collector.py --finalize --json  # 9:25 收口并筛出竞价前 20

即时/调试：
  python auction_collector.py --codes sh600519 --once --json   # 单次抓取+计算（不落盘）
  python auction_collector.py --example --json                  # 合成竞价数据演示
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))
from a_stock_http import DataSourceError  # noqa: E402
from market_adapters import fetch_tencent_snapshot  # noqa: E402
from announcement_risk import scan_many  # noqa: E402
from a_share_rules import add_trading_days  # noqa: E402
import candidate_lifecycle  # noqa: E402
import candidate_pipeline  # noqa: E402
import hot_money_selection  # noqa: E402
import stage_intelligence  # noqa: E402
import monitor_registry  # noqa: E402
from recommendation_quality import build_execution_plan, build_quality_report  # noqa: E402
from decision_policy import evaluate_decision  # noqa: E402
from market_snapshot import compact_ref, materialize_input_snapshot  # noqa: E402
from market_context import market_regime, read_market_context  # noqa: E402
from portfolio_policy import evaluate_candidate, portfolio_value  # noqa: E402
from research_evidence import build_research_evidence  # noqa: E402
import signal_ledger  # noqa: E402
import strategy_registry  # noqa: E402
import trading_discipline  # noqa: E402
from tradeability import limit_pct, round_limit  # noqa: E402
from state_store import atomic_write_json, mutate_json, read_json  # noqa: E402
from signal_context import read_signal_context  # noqa: E402
from paths import data_file  # noqa: E402
from config_registry import load_registered  # noqa: E402

AUCTION_OPEN_FREEZE = "09:20"  # 9:20 后委托不可撤单，9:20→9:25 委买净增 = 无撤单窗口真实意图
QUOTE_BATCH_SIZE = 80
MAX_POOL_AGE_DAYS = 4
AUCTION_WINDOW_START_MINUTE = 9 * 60 + 15
AUCTION_WINDOW_END_MINUTE = 9 * 60 + 25
AUCTION_EXPECTED_TIMEPOINTS = AUCTION_WINDOW_END_MINUTE - AUCTION_WINDOW_START_MINUTE + 1
QUALITY_MIN_NONZERO_RATE = 0.5
QUALITY_MAX_MIRROR_RATE = 0.5
QUALITY_MIN_TIME_COVERAGE = 0.5
DEFAULT_SHORTLIST_LIMIT = int(
    load_registered("candidate_selection")["pipeline"]["auction_shortlist_limit"]
)


class AuctionQuality(dict):
    """Structured quality state, with equality compatibility for old callers."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            # Existing integrations used ok/degraded strings. Keep those
            # comparisons readable while JSON callers receive the real state.
            return self.get("status") == other or (
                self.get("status") == "unavailable" and other in {"ok", "degraded"}
            )
        return super().__eq__(other)


def _minute_slot(value: Any) -> Optional[int]:
    text = str(value or "")[:5]
    try:
        hour, minute = (int(part) for part in text.split(":", 1))
    except (TypeError, ValueError):
        return None
    slot = hour * 60 + minute
    return slot if AUCTION_WINDOW_START_MINUTE <= slot <= AUCTION_WINDOW_END_MINUTE else None


def _is_mirrored_book(snap: Mapping[str, Any]) -> bool:
    bids = list(snap.get("bids") or [])
    asks = list(snap.get("asks") or [])
    if not bids or not asks:
        return False
    def normalized(levels: Any) -> List[Tuple[Optional[float], Optional[float]]]:
        return [
            (None if level[0] is None else round(float(level[0]), 6),
             None if level[1] is None else round(float(level[1]), 6))
            for level in levels
            if isinstance(level, (tuple, list)) and len(level) >= 2
        ]
    bid_levels = normalized(bids)
    ask_levels = normalized(asks)
    if not bid_levels or not ask_levels:
        return False
    # Some free feeds mirror only quantities while changing the displayed prices.
    return bid_levels == ask_levels or [v for _, v in bid_levels] == [v for _, v in ask_levels]


def _has_valid_book(snap: Mapping[str, Any]) -> bool:
    def has_level(levels: Any) -> bool:
        return any(
            isinstance(level, (tuple, list))
            and len(level) >= 2
            and (level[0] is not None or (level[1] or 0) > 0)
            for level in (levels or [])
        )
    return has_level(snap.get("bids")) and has_level(snap.get("asks"))


def _quality_for_snapshots(snapshots: List[Dict[str, Any]]) -> AuctionQuality:
    total = len(snapshots)
    nonzero_count = sum(1 for snap in snapshots if (snap.get("volume") or 0) > 0)
    book_count = sum(1 for snap in snapshots if _has_valid_book(snap))
    mirror_count = sum(1 for snap in snapshots if _is_mirrored_book(snap))
    slots = {_minute_slot(snap.get("t")) for snap in snapshots}
    slots.discard(None)
    nonzero_rate = nonzero_count / total if total else 0.0
    mirror_rate = mirror_count / book_count if book_count else None
    # Coverage is the fraction of supplied observations with a valid auction
    # timestamp. The window-relative value is also retained for operations;
    # a one-shot quote is valid data but is not mistaken for a full 09:15-09:25
    # series.
    time_coverage_rate = len(slots) / total if total else 0.0
    window_coverage_rate = len(slots) / AUCTION_EXPECTED_TIMEPOINTS
    reasons: List[str] = []
    if nonzero_rate < QUALITY_MIN_NONZERO_RATE:
        reasons.append("竞价量能非零率低于质量门槛")
    if mirror_rate is None:
        reasons.append("五档盘口无有效覆盖")
    elif mirror_rate > QUALITY_MAX_MIRROR_RATE:
        reasons.append("五档委买卖镜像率超过质量门槛")
    if time_coverage_rate < QUALITY_MIN_TIME_COVERAGE:
        reasons.append("竞价时点覆盖率低于质量门槛")
    status = "unavailable" if reasons else "ok"
    return AuctionQuality({
        "status": status,
        "nonzero_rate": round(nonzero_rate, 4),
        "mirror_rate": round(mirror_rate, 4) if mirror_rate is not None else None,
        "time_coverage_rate": round(time_coverage_rate, 4),
        "window_coverage_rate": round(window_coverage_rate, 4),
        "snapshot_count": total,
        "nonzero_volume_count": nonzero_count,
        "book_count": book_count,
        "mirrored_book_count": mirror_count,
        "timepoints_covered": len(slots),
        "expected_timepoints": AUCTION_EXPECTED_TIMEPOINTS,
        "reasons": reasons,
    })


def _quality_status(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("status") or "unavailable")
    return str(value or "unavailable")


def _sum_vol(levels: List[Tuple[Optional[float], Optional[float]]]) -> float:
    return sum(v for _, v in levels if v)


def _snapshot_bid_vol(snap: Dict[str, Any]) -> float:
    return _sum_vol(snap.get("bids", []))


def _net_bid_delta(snapshots: List[Dict[str, Any]]) -> Optional[float]:
    """9:20→9:25 委买净增量（手）。需 >=2 个快照且跨越 9:20，否则 None。"""
    post_freeze = [s for s in snapshots if str(s.get("t", "")) >= AUCTION_OPEN_FREEZE]
    if len(post_freeze) < 2:
        return None
    return round(_snapshot_bid_vol(post_freeze[-1]) - _snapshot_bid_vol(post_freeze[0]), 1)


def _gap_series(snapshots: List[Dict[str, Any]], prev_close: float) -> List[float]:
    """竞价指示价相对昨收的涨跌幅序列（跳过缺价快照）。"""
    series: List[float] = []
    for snap in snapshots:
        price = snap.get("price")
        base = snap.get("prev_close") or prev_close
        if price is None or not base:
            continue
        series.append(round((price - base) / base * 100, 2))
    return series


def _data_quality_notes(snapshots: List[Dict[str, Any]], volume: float) -> List[str]:
    """免费源已知局限：竞价量能恒 0、五档委买委卖完全镜像 → 盘口不可当真信号。"""
    notes: List[str] = []
    if volume <= 0:
        notes.append("竞价量能为0，免费源未提供竞价成交量")
    books = [
        (_snapshot_bid_vol(snap), _sum_vol(snap.get("asks", [])))
        for snap in snapshots
        if snap.get("bids") or snap.get("asks")
    ]
    real_books = [(bid, ask) for bid, ask in books if bid > 0 or ask > 0]
    if real_books and all(abs(bid - ask) < 1e-6 for bid, ask in real_books):
        notes.append("五档委买与委卖全时点完全相等，盘口疑似镜像填充")
    return notes


def compute_auction_factors(snapshots: List[Dict[str, Any]], code: str, name: str = "") -> Dict[str, Any]:
    """从一只票的竞价快照序列算出真竞价因子（纯函数，不触网）。

    除静态因子外还给出竞价轨迹：`auction_max_gap_pct` / `auction_price_decay_pct`
    量化 9:15→9:25 指示价从高点的回落幅度，用于识别诱多出货（issue #139/#140）。
    """
    if not snapshots:
        return {"code": code, "name": name, "error": "无竞价快照"}
    last = snapshots[-1]
    price = last.get("price")
    prev_close = last.get("prev_close")
    if price is None or prev_close in (None, 0):
        return {"code": code, "name": name, "error": "缺少现价/昨收，无法计算竞价因子"}

    bid_vol = _snapshot_bid_vol(last)
    ask_vol = _sum_vol(last.get("asks", []))
    best_bid_vol = (last.get("bids") or [(None, None)])[0][1] or 0.0
    raw_volume = last.get("volume")
    volume = raw_volume if raw_volume is not None else 0.0  # 竞价累计成交量（手）
    market_cap_yi = last.get("market_cap")       # 流通市值（亿元）

    gap_pct = round((price - prev_close) / prev_close * 100, 2)
    pct = limit_pct(code, name)
    limit_up = round_limit(prev_close, pct, up=True)
    limit_down = round_limit(prev_close, pct, up=False)
    at_limit = price >= limit_up - 1e-6
    at_limit_down = price <= limit_down + 1e-6

    gaps = _gap_series(snapshots, prev_close) or [gap_pct]
    max_gap_pct = max(gaps)
    decay_pct = round(max(0.0, max_gap_pct - gap_pct), 2)
    limit_up_gap_pct = round((limit_up - prev_close) / prev_close * 100, 2)
    faded_from_limit_up = not at_limit and max_gap_pct >= limit_up_gap_pct - 1e-6

    if at_limit_down:
        board_status = "limit_down"         # 竞价跌停，买入无意义
    elif at_limit and ask_vol == 0:
        board_status = "yizi_seal"          # 竞价一字封死
    elif at_limit:
        board_status = "limit_up_with_ask"  # 竞价上板但有卖盘（T字/可撬）
    elif gap_pct > 0:
        board_status = "high_open"
    else:
        board_status = "flat_or_low_open"

    quality_notes = _data_quality_notes(snapshots, volume)
    quality = _quality_for_snapshots(snapshots)

    seal_ratio_pct = None
    if at_limit and market_cap_yi:
        seal_ratio_pct = round(best_bid_vol * 100 * price / (market_cap_yi * 1e8) * 100, 3)

    return {
        "code": code,
        "name": name,
        "indicative_price": price,
        "prev_close": prev_close,
        "auction_gap_pct": gap_pct,
        "auction_max_gap_pct": max_gap_pct,
        "auction_price_decay_pct": decay_pct,
        "auction_faded_from_limit_up": faded_from_limit_up,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "auction_volume": round(volume, 1) if raw_volume is not None else None,
        "prev_day_volume": last.get("prev_day_volume"),
        "auction_amount": round(price * volume * 100, 0) if raw_volume is not None else None,
        "prev_day_amount": last.get("prev_day_amount"),
        "limitup_day_volume": last.get("limitup_day_volume"),
        "auction_volume_prev_day_ratio": (
            round(volume / float(last["prev_day_volume"]), 4)
            if raw_volume is not None and last.get("prev_day_volume") else None
        ),
        "auction_amount_prev_day_ratio": (
            round((price * volume * 100) / float(last["prev_day_amount"]), 4)
            if raw_volume is not None and last.get("prev_day_amount") else None
        ),
        "auction_volume_limitup_day_ratio": (
            round(volume / float(last["limitup_day_volume"]), 4)
            if raw_volume is not None and last.get("limitup_day_volume") else None
        ),
        # Tencent's free snapshot does not expose these L2 fields. Preserve
        # their schema and fail closed instead of manufacturing a signal.
        "unmatched_volume_after_0920": last.get("unmatched_volume_after_0920"),
        "post_0920_unmatched_volume": last.get("post_0920_unmatched_volume"),
        "auction_unmatched_volume_after_0920": last.get("auction_unmatched_volume_after_0920"),
        "seal_stability": last.get("seal_stability"),
        "auction_seal_stability": last.get("auction_seal_stability"),
        "auction_bid_ask_ratio": round(bid_vol / ask_vol, 2) if ask_vol else None,
        "auction_net_bid_delta": _net_bid_delta(snapshots),
        "board_status": board_status,
        "seal_amount_ratio_pct": seal_ratio_pct,
        "snapshots_used": len(snapshots),
        "is_yiziban": board_status == "yizi_seal",
        "is_limit_down": at_limit_down,
        "auction_data_quality": quality,
        "auction_data_quality_notes": quality_notes,
    }


def take_snapshot(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """抓一次腾讯实时+五档，打上时间戳。"""
    quotes: Dict[str, Dict[str, Any]] = {}
    for index in range(0, len(codes), QUOTE_BATCH_SIZE):
        quotes.update(fetch_tencent_snapshot(codes[index:index + QUOTE_BATCH_SIZE]))
    now = datetime.now().strftime("%H:%M:%S")
    for q in quotes.values():
        q["t"] = now
    return quotes


def _code_set(values: Iterable[Any]) -> set[str]:
    result: set[str] = set()
    for value in values:
        raw = value.get("code") if isinstance(value, Mapping) else value
        if raw:
            result.add(candidate_pipeline.naked_code(raw))
    return result


def _quote_change_pct(quote: Mapping[str, Any]) -> Optional[float]:
    try:
        if quote.get("change_pct") is not None:
            return float(quote["change_pct"])
        previous = float(quote.get("prev_close") or 0)
        price = float(quote.get("price") or 0)
        return (price - previous) / previous * 100 if previous > 0 else None
    except (TypeError, ValueError):
        return None


def is_recall_target_event(quote: Mapping[str, Any]) -> bool:
    """Return whether a 09:24 quote is a measurable strong-board event.

    The monitor is deliberately descriptive: it recognises an explicit
    provider flag when available, otherwise uses the quote's limit percentage
    and a conservative near-limit threshold.  It never feeds this flag into
    ranking or execution gates.
    """
    if any(quote.get(key) is True for key in ("target_event", "is_limit_up", "strong_board")):
        return True
    change = _quote_change_pct(quote)
    if change is None:
        return False
    code = candidate_pipeline.naked_code(quote.get("code"))
    name = str(quote.get("name") or "")
    try:
        limit_gap = float(limit_pct(code, name))
    except (TypeError, ValueError):
        limit_gap = 10.0
    # A near-limit event captures both ordinary 10cm boards and 20cm boards;
    # the 7% floor keeps the metric useful for strong (but not yet sealed)
    # boards while excluding routine small advances.
    return change >= 7.0 or change >= min(9.5, limit_gap - 0.5)


def annotate_recall_snapshot(
    quotes: Mapping[str, Mapping[str, Any]],
    pool: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Annotate a full-market 09:24 snapshot for recall accounting only."""
    prefilter_codes = _code_set(pool.get("prefilter_codes") or [])
    eligible_codes = _code_set(pool.get("full_market_codes") or [])
    if not eligible_codes:
        # Backward-compatible fallback for pools produced before this monitor.
        eligible_codes = _code_set(pool.get("auction_scan_codes") or [])
    annotated: Dict[str, Dict[str, Any]] = {}
    for raw_code, raw_quote in quotes.items():
        quote = dict(raw_quote)
        code = candidate_pipeline.naked_code(raw_quote.get("code") or raw_code)
        quote["code"] = code
        target = is_recall_target_event(quote)
        outside = target and code not in prefilter_codes
        quote.update({
            "recall_target_event": target,
            "outside_pool_strong": outside,
            # This is an ex-post counterfactual label only.  It does not add
            # the row to candidates, auction ranking, or execution output.
            "would_have_been_candidate": bool(outside and code in eligible_codes),
            "snapshot_scope": "full_market",
        })
        annotated[raw_code] = quote
    return annotated


def _recall_rate(covered: int, total: int) -> Optional[float]:
    return round(covered / total, 4) if total else None


def build_discovery_recall_report(
    quotes: Iterable[Mapping[str, Any]],
    *,
    prefilter_codes: Iterable[Any],
    auction_codes: Iterable[Any],
    executable_codes: Iterable[Any] | None = None,
    open_codes: Iterable[Any] | None = None,
    asof: str,
    source_stage: str = "09:24_full_market",
) -> Dict[str, Any]:
    """Build the bounded, fail-closed recall report for one trading date."""
    rows = [dict(item) for item in quotes]
    target_codes = {
        candidate_pipeline.naked_code(item.get("code"))
        for item in rows
        if item.get("code") and is_recall_target_event(item)
    }
    prefilter = _code_set(prefilter_codes)
    auction = _code_set(auction_codes)
    executable = _code_set(executable_codes or [])
    opened = _code_set(open_codes or []) if open_codes is not None else None

    def stage_payload(codes: set[str], *, available: bool = True) -> Dict[str, Any]:
        covered = len(target_codes & codes)
        return {
            "available": available,
            "target_count": len(target_codes),
            "covered_count": covered if available else None,
            "lost_count": (len(target_codes) - covered) if available else None,
            "recall": _recall_rate(covered, len(target_codes)) if available else None,
            "covered_codes": sorted(target_codes & codes) if available else [],
        }

    d0 = stage_payload(prefilter)
    auction_stage = stage_payload(auction)
    executable_stage = stage_payload(executable, available=executable_codes is not None)
    open_stage = stage_payload(opened or set(), available=opened is not None)
    outside = sorted(target_codes - prefilter)
    loss_by_stage = {
        "d0_prefilter_loss_count": d0["lost_count"],
        "auction_pool_loss_count": auction_stage["lost_count"],
        "executable_loss_count": executable_stage["lost_count"],
        "open_confirmation_loss_count": open_stage["lost_count"],
    }
    staged_loss = {
        "target_count": len(target_codes),
        "outside_pool_strong_count": len(outside),
        "outside_pool_strong_codes": outside[:200],
        "loss_by_stage": loss_by_stage,
        "d0_prefilter": d0,
        "auction": auction_stage,
        "open": open_stage,
        "d0_to_auction_lost_count": len((target_codes & prefilter) - auction),
        "auction_to_open_lost_count": (
            len((target_codes & auction) - (opened or set())) if opened is not None else None
        ),
        "open_pending": opened is None,
    }
    return {
        "schema": "discovery_recall_report_v1",
        "asof": asof,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_stage": source_stage,
        "status": "ready" if opened is not None else "pending",
        "target_event": "near_limit_or_strong_board",
        "target_count": len(target_codes),
        "discovery_recall": d0["recall"],
        "auction_recall": auction_stage["recall"],
        "executable_recall": executable_stage["recall"],
        "open_recall": open_stage["recall"],
        "staged_loss": staged_loss,
        "coverage": {
            "d0_prefilter": d0,
            "auction_pool": auction_stage,
            "executable": executable_stage,
            "open_confirmation": open_stage,
        },
        "outside_pool_strong_count": len(outside),
        "outside_pool_strong_codes": outside[:200],
        "would_have_been_candidate_count": sum(
            1
            for item in rows
            if item.get("would_have_been_candidate") is True
            or (
                "would_have_been_candidate" not in item
                and item.get("code")
                and candidate_pipeline.naked_code(item.get("code")) in outside
            )
        ),
        "execution_gate_unchanged": True,
        "note": "召回统计仅用于优化D0预筛阈值，不扩大竞价池，不进入执行排名",
    }


def _state_path(asof: str) -> str:
    return data_file("daban-stock-picker", f"auction_{asof}.json")


def _pool_path() -> str:
    return data_file("stock-triage", "candidate_pool_latest.json")


def _shortlist_path(asof: str) -> str:
    return data_file("daban-stock-picker", f"auction_shortlist_{asof}.json")


def _shortlist_latest_path() -> str:
    return data_file("daban-stock-picker", "auction_shortlist_latest.json")


def load_watch_pool(event_asof: str | None = None) -> Dict[str, Any]:
    pool = read_json(_pool_path(), {})
    if not isinstance(pool, dict) or pool.get("status") != "ready":
        raise DataSourceError("candidate_pool", "动态观察池缺失或不可用，请先运行 candidate-discovery")
    # 弱市时 candidates 可能为空（research_only），但 auction_scan_codes 仍有数据
    # 此时不报错，让下游 auction_scan_codes() 自行回退
    if not pool.get("candidates") and not pool.get("auction_scan_codes"):
        raise DataSourceError("candidate_pool", "动态观察池缺失或不可用，请先运行 candidate-discovery")
    if event_asof:
        try:
            age = (
                datetime.fromisoformat(event_asof).date()
                - datetime.fromisoformat(str(pool.get("asof"))).date()
            ).days
        except ValueError as exc:
            raise DataSourceError("candidate_pool", "动态观察池缺少有效日期", exc) from exc
        if age < 0 or age > MAX_POOL_AGE_DAYS:
            raise DataSourceError("candidate_pool", f"动态观察池已过期: source={pool.get('asof')}")
    return pool


def watch_pool_codes(pool: Mapping[str, Any]) -> List[str]:
    return [
        candidate_pipeline.market_code(item.get("code") or item.get("market_code"))
        for item in pool.get("candidates", [])
        if item.get("code") or item.get("market_code")
    ]


def auction_scan_codes(
    pool: Mapping[str, Any],
    *,
    full_universe: bool,
) -> List[str]:
    """Return deep-pool codes or the full eligible one-shot scan universe.

    弱市时 candidate-discovery 会把候选整体降级为 research_only，此时
    ``candidates`` 为空而 ``auction_scan_codes`` 仍有数据。若直接返回空列表，
    竞价将扫 0 只股票，市场温度退化为 stale 值（issue #112 / #113）。
    因此深池模式在 ``candidates`` 为空时回退到 ``auction_scan_codes``。
    """
    # The full-market 09:24 observation is separate from the bounded auction
    # pool.  Keep the old field as a fallback for legacy pool artifacts.
    source = (
        pool.get("full_market_codes") or pool.get("auction_scan_codes")
        if full_universe
        else None
    )
    if not source:
        codes = watch_pool_codes(pool)
        if codes:
            return codes
        source = pool.get("auction_scan_codes")
    if not source:
        return []
    return list(dict.fromkeys(
        candidate_pipeline.market_code(code)
        for code in source
        if code
    ))


def append_snapshot(
    codes: List[str],
    asof: str,
    *,
    full_universe: bool = False,
) -> Dict[str, Any]:
    """事务式把一次快照追加到当日状态文件（单锁 read-modify-write）。"""
    raw_quotes = take_snapshot(codes)
    if full_universe:
        # Intelligence-only annotation; it must never mutate the executable
        # candidate or auction pool.
        raw_quotes = annotate_recall_snapshot(raw_quotes, load_watch_pool(asof))
    input_snapshot = materialize_input_snapshot(
        "auction-quote-input",
        {
            "schema": "auction_quote_inputs_v1",
            "quotes": raw_quotes,
        },
        trading_date=asof,
        batch_id=os.environ.get("A_STOCK_BATCH_ID") or f"a-share-{asof.replace('-', '')}",
        producer="auction-snapshot",
        source_versions={"tencent": "tencent-adapter-v2"},
    )
    quotes = dict(input_snapshot["payload"]["quotes"])
    snapshot_ref = compact_ref(input_snapshot)

    def mutator(state: Dict[str, Any]) -> Dict[str, Any]:
        state.setdefault("asof", asof)
        refs = state.setdefault("input_snapshots", [])
        refs.append(snapshot_ref)
        state["input_snapshots"] = refs[-20:]
        series = state.setdefault("series", {})
        for code, q in quotes.items():
            series.setdefault(code, []).append(q)
        if full_universe:
            state["full_market_snapshot_at"] = datetime.now().isoformat(timespec="seconds")
            state["full_market_snapshot_count"] = len(quotes)
        return state

    return mutate_json(_state_path(asof), mutator, default={"asof": asof, "series": {}})


def _rejection_map(records: Iterable[Mapping[str, Any]]) -> Dict[str, List[str]]:
    return {
        candidate_pipeline.naked_code(item.get("code")): list(item.get("rejection_reasons") or [])
        for item in records
        if item.get("code")
    }


def _persist_shortlist(result: Mapping[str, Any], asof: str) -> None:
    atomic_write_json(_shortlist_path(asof), dict(result))
    atomic_write_json(_shortlist_latest_path(), dict(result))
    quality_report = result.get("auction_quality_report")
    if quality_report is not None:
        atomic_write_json(
            data_file("daban-stock-picker", "auction_quality_report_latest.json"),
            dict(quality_report),
        )


def _degraded_finalize(result: Dict[str, Any], asof: str, reason: str) -> Dict[str, Any]:
    """竞价采集为空时的 fail-closed 结果。

    零因子不等于"今天没有机会"，而是"今天没有观测"。若照常报 ready，下游
    （市场温度、仓位乘数、打板门禁）会把空结果当成有效观测并沿用 stale 上下文
    （issue #112 / #113）。因此显式降级，且不注册任何监控、不推进候选生命周期。
    """
    try:
        pool: Mapping[str, Any] = load_watch_pool(asof)
    except DataSourceError:
        pool = {}
    result.update({
        "schema": "auction_finalize_v2",
        "asof": asof,
        "status": "degraded",
        "collection_status": "empty",
        "research_only": True,
        "degraded_reasons": [reason],
        "source_asof": pool.get("asof"),
        "input_count": len(pool.get("candidates") or []),
        "factor_count": 0,
        "shortlist": [],
        "shortlist_count": 0,
        "rejected": [],
        "preopen_decisions": [],
        "decision_count": 0,
        "discipline_state": None,
        "auction_quality": AuctionQuality({
            "status": "unavailable",
            "nonzero_rate": 0.0,
            "mirror_rate": None,
            "time_coverage_rate": 0.0,
            "reasons": [reason],
        }),
        "auction_quality_report": AuctionQuality({
            "status": "unavailable",
            "nonzero_rate": 0.0,
            "mirror_rate": None,
            "time_coverage_rate": 0.0,
            "reasons": [reason],
        }),
    })
    _persist_shortlist(result, asof)
    return result


def finalize(asof: str, shortlist_limit: int = DEFAULT_SHORTLIST_LIMIT) -> Dict[str, Any]:
    state = read_json(_state_path(asof), default={"series": {}})
    result = _build_result(state.get("series", {}), asof)
    if not result["factors"]:
        return _degraded_finalize(
            result,
            asof,
            "竞价采集为空（0 只标的），无盘中观测，拒绝输出可执行结论",
        )
    pool = load_watch_pool(asof)
    signal_ctx = read_signal_context(max_age_hours=8) or {}
    shortlist = candidate_pipeline.rank_auction_shortlist(
        pool,
        result["factors"],
        limit=shortlist_limit,
        signal_ctx=signal_ctx,
    )
    aggregate_quality = result.get("auction_quality")
    shortlist_quality = shortlist.get("auction_quality")
    result.update(shortlist)
    # Keep the rate-bearing collector report as the canonical artifact field;
    # candidate_pipeline's compact ranking summary is retained alongside it.
    if isinstance(aggregate_quality, Mapping):
        aggregate_quality["ranking_summary"] = shortlist_quality
        result["auction_quality"] = aggregate_quality
    result["social_attention_snapshot"] = signal_ctx.get(
        "social_attention_snapshot"
    )
    result["schema"] = "auction_finalize_v2"
    result["asof"] = asof
    result["status"] = "ready"
    result["research_only"] = False
    top_candidates = list(result["shortlist"][:5])
    announcement_map = scan_many(
        candidate_pipeline.naked_code(item.get("code"))
        for item in top_candidates
    )
    decisions = []
    portfolio = read_json(data_file("stock-triage", "portfolio.json"), {})
    regime = market_regime(read_market_context())
    discipline_state = trading_discipline.assess_discipline_state(
        signal_ledger.read_events(),
        total_assets=portfolio_value(portfolio),
        asof=asof,
    )
    monitor_expiry = add_trading_days(asof, 2)
    batch_id = os.environ.get("A_STOCK_BATCH_ID") or f"a-share-{asof.replace('-', '')}"
    stock_monitor_targets = []
    sector_monitor_targets: dict[str, dict[str, Any]] = {}
    for item in top_candidates:
        code = candidate_pipeline.naked_code(item.get("code"))
        provisional = build_execution_plan(
            {**item, "action": "trend_watch", "price": item.get("indicative_price")},
            {"status": "passed", "execution_constraints": {}},
            asof=asof,
            stage="auction",
        )
        recommendation = {
            **item,
            "action": "buy",
            "entry_price": item.get("indicative_price"),
            "price_range": provisional["entry_range"],
            "stop_price": provisional["stop_price"],
            "target_price": provisional["target_price"],
            "horizon": provisional["horizon"],
            "grade": "A" if float(item.get("auction_score") or 0) >= 80 else "B",
            "confidence": "medium",
            "position_pct": provisional["position_pct"] or 4.0,
            "tradeability": {"tradeable": not item.get("is_yiziban"), "status": "auction"},
        }
        quality = build_quality_report(
            recommendation,
            announcement_map.get(code),
            asof=asof,
        )
        plan = build_execution_plan(
            {**item, "action": "trend_watch", "price": item.get("indicative_price")},
            quality,
            asof=asof,
            stage="auction",
        )
        selected_lane = (
            "daban"
            if (item.get("auction_selected_by") or {}).get("daban")
            else "trend"
        )
        strategy_id = hot_money_selection.selection_strategy_id(
            item,
            selected_lane,
        )
        lane = "daban" if strategy_id.startswith("daban") else "trend"
        selection_context = hot_money_selection.advance_selection_context(
            item,
            window="09:25",
        )
        evidence = build_research_evidence(code, strategy_id=strategy_id, asof=asof)
        prior_chanlun = ((item.get("research_evidence") or {}).get("chanlun") or {})
        for key in (
            "structure_summary",
            "signals",
            "live_bullish_signals",
            "live_bearish_signals",
            "display_only_signals",
        ):
            if key in prior_chanlun:
                evidence["chanlun"][key] = prior_chanlun[key]
        portfolio_risk = evaluate_candidate(
            portfolio,
            item,
            float(plan.get("position_pct") or 0),
        )
        policy = evaluate_decision(
            requested_action=plan["decision"],
            quality_report=quality,
            strategy_record=strategy_registry.live_record(strategy_id),
            market_regime=regime,
            portfolio_risk=portfolio_risk,
            research_evidence=evidence,
            strategy_lane=lane,
            market_crowding=selection_context.get("market_timing") or {},
            discipline_state=discipline_state,
        )
        if policy["decision"] in {"avoid", "watch"}:
            plan["decision"] = policy["decision"]
            plan["position_pct"] = 0.0
        decision = {
            **item,
            "decision": plan["decision"],
            "execution_plan": plan,
            "quality_report": quality,
            "policy_decision": policy,
            "portfolio_risk": portfolio_risk,
            "research_evidence": evidence,
            "market_regime": regime,
            "discipline_state": discipline_state,
            "strategy_id": strategy_id,
            "selection_context": selection_context,
            "announcements": announcement_map.get(code),
        }
        decisions.append(decision)
        if plan["decision"] != "avoid":
            stock_monitor_targets.append({
                "code": code,
                "name": str(item.get("name") or code),
                "metadata": {
                    "decision": plan["decision"],
                    "auction_rank": item.get("auction_rank"),
                    "quality_status": quality["status"],
                    "strategy_id": strategy_id,
                    "sector_rank": item.get("sector_rank"),
                    "leader_rank": item.get("leader_rank"),
                },
            })
        sector = str(item.get("sector") or "").strip()
        if sector and plan["decision"] != "avoid":
            sector_monitor_targets[sector] = {"key": sector, "label": sector}
    monitor_registry.reconcile_automatic(
        "stock",
        stock_monitor_targets,
        source="auction_finalize",
        source_group="auction_shortlist",
        trading_date=asof,
        batch_id=batch_id,
        expires_at=monitor_expiry,
    )
    monitor_registry.reconcile_automatic(
        "sector",
        sector_monitor_targets.values(),
        source="auction_finalize",
        source_group="auction_shortlist",
        trading_date=asof,
        batch_id=batch_id,
        expires_at=monitor_expiry,
    )
    result["preopen_decisions"] = decisions
    result["decision_count"] = len(decisions)
    result["discipline_state"] = discipline_state
    _persist_shortlist(result, asof)

    selected = [item["code"] for item in result["shortlist"]]
    decisions_by_code = {
        candidate_pipeline.naked_code(item["code"]): item for item in decisions
    }
    candidate_lifecycle.transition(
        str(pool["asof"]),
        "auction_shortlist",
        selected,
        rejection_reasons=_rejection_map(result["rejected"]),
        event_asof=asof,
        details_by_code={
            candidate_pipeline.naked_code(item["code"]): {
                "auction_rank": item["auction_rank"],
                "auction_score": item["auction_score"],
                "auction_sector_rank": item.get("auction_sector_rank"),
                "hot_money_qualified": item.get("hot_money_qualified"),
                "reflexivity": dict(
                    (
                        decisions_by_code.get(candidate_pipeline.naked_code(item["code"]), {})
                        .get("selection_context", {})
                        .get("market_timing", {})
                        .get("reflexivity", {})
                    )
                ),
                "policy_reasons": list(
                    decisions_by_code.get(candidate_pipeline.naked_code(item["code"]), {})
                    .get("policy_decision", {})
                    .get("reasons", [])
                ),
                "position_multiplier": (
                    decisions_by_code.get(candidate_pipeline.naked_code(item["code"]), {})
                    .get("policy_decision", {})
                    .get("position_multiplier")
                ),
            }
            for item in result["shortlist"]
        },
    )
    return result


def _build_result(series: Dict[str, List[Dict[str, Any]]], asof: str) -> Dict[str, Any]:
    factors = [
        compute_auction_factors(snaps, code, (snaps[-1].get("name") if snaps else "") or "")
        for code, snaps in series.items()
    ]
    quality = _quality_for_snapshots([
        snap for snapshots in series.values() for snap in snapshots
    ])
    factor_quality_reasons = [
        reason
        for factor in factors
        for reason in (
            factor.get("auction_data_quality")
            if isinstance(factor.get("auction_data_quality"), Mapping)
            else {}
        ).get("reasons", [])
    ]
    if any(
        _quality_status(factor.get("auction_data_quality")) == "unavailable"
        for factor in factors
    ):
        quality["status"] = "unavailable"
        quality["reasons"] = list(dict.fromkeys(
            list(quality.get("reasons") or [])
            + factor_quality_reasons
            + ["至少一个标的竞价质量不可用"]
        ))
    return {
        "schema": "auction_factors_v1",
        "asof": asof,
        "generated_at": datetime.now().isoformat(),
        "note": "免费腾讯五档竞价因子；撤单率类信号需 L2；阈值须经 chanlun-backtest 验证后方可实盘",
        "factors": factors,
        "auction_quality": quality,
        "auction_quality_report": quality,
    }


def example_result() -> Dict[str, Any]:
    """合成竞价快照：一字封死 + 高开放量，验证因子计算，无需开盘/触网。"""
    series = {
        "sh600001": [
            {"t": "09:18:00", "name": "示例股份", "price": 11.0, "prev_close": 10.0,
             "volume": 8000, "market_cap": 80.0,
             "bids": [(11.0, 50000), (None, None), (None, None), (None, None), (None, None)],
             "asks": [(None, None)] * 5},
            {"t": "09:24:50", "name": "示例股份", "price": 11.0, "prev_close": 10.0,
             "volume": 12000, "market_cap": 80.0,
             "bids": [(11.0, 90000), (None, None), (None, None), (None, None), (None, None)],
             "asks": [(None, None)] * 5},
        ],
        "sh600111": [
            {"t": "09:21:00", "name": "北方稀土", "price": 21.5, "prev_close": 20.0,
             "volume": 30000, "market_cap": 300.0,
             "bids": [(21.5, 4000), (21.49, 3000), (21.48, 2000), (21.47, 1000), (21.46, 800)],
             "asks": [(21.51, 6000), (21.52, 5000), (21.53, 4000), (21.54, 3000), (21.55, 2000)]},
        ],
    }
    return _build_result(series, "2026-06-04")


def json_report(result: Mapping[str, Any]) -> Dict[str, Any]:
    decisions = list(result.get("preopen_decisions") or [])
    report = {
        "schema": result.get("schema"),
        "status": result.get("status", "ready"),
        "asof": result.get("asof"),
        "generated_at": result.get("generated_at"),
        "source_asof": result.get("source_asof"),
        # 曾硬编码 False，使降级报告自相矛盾（status=degraded 却 research_only=False）
        "research_only": bool(result.get("research_only", False)),
        "degraded_reasons": list(result.get("degraded_reasons") or []),
        "collection_status": result.get("collection_status"),
        "auction_quality": result.get("auction_quality") or result.get("auction_quality_report"),
        "auction_quality_report": result.get("auction_quality_report"),
        "input_count": result.get("input_count"),
        "shortlist_count": result.get("shortlist_count", len(result.get("shortlist") or [])),
        "decision_count": result.get("decision_count", len(decisions)),
        "discipline_state": result.get("discipline_state"),
        "top_candidates": [
            {
                "code": item.get("code"),
                "name": item.get("name"),
                "sector": item.get("sector"),
                "sector_rank": item.get("sector_rank"),
                "leader_rank": item.get("leader_rank"),
                "auction_rank": item.get("auction_rank"),
                "auction_sector_rank": item.get("auction_sector_rank"),
                "auction_score": item.get("auction_score"),
                "strategy_id": item.get("strategy_id"),
                "hot_money_qualified": item.get("hot_money_qualified"),
                "decision": item.get("decision"),
                "auction_quality": item.get("auction_quality") or item.get("auction_data_quality"),
                "quality_status": (item.get("quality_report") or {}).get("status"),
                "policy_reasons": list((item.get("policy_decision") or {}).get("reasons") or []),
                "selection_context": hot_money_selection.compact_selection_context(
                    item.get("selection_context")
                ),
            }
            for item in decisions[:5]
        ],
    }
    report["intelligence"] = stage_intelligence.auction_digest(result)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="A股集合竞价真竞价因子采集器")
    parser.add_argument("--codes", help="逗号分隔，带市场前缀，如 sh600519,sz000001")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--snapshot", action="store_true", help="抓一次快照并落盘（cron 多次调用）")
    parser.add_argument(
        "--full-universe",
        action="store_true",
        help="仅本次快照覆盖候选发现的全部合格股票；用于09:24轻量异动扫描",
    )
    parser.add_argument("--finalize", action="store_true", help="读当日快照算因子")
    parser.add_argument("--once", action="store_true", help="单次抓取+计算，不落盘（调试）")
    parser.add_argument("--example", action="store_true", help="合成数据演示，不触网")
    parser.add_argument(
        "--shortlist-limit",
        type=int,
        default=DEFAULT_SHORTLIST_LIMIT,
        help="竞价收口保留数量（默认读取 candidate_selection 配置）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",")] if args.codes else []

    if args.example:
        result = example_result()
    elif args.once:
        if not codes:
            parser.error("--once 需要 --codes")
        try:
            series = {code: [snap] for code, snap in take_snapshot(codes).items()}
        except DataSourceError as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
            sys.exit(1)
        result = _build_result(series, args.asof)
    elif args.snapshot:
        if not codes:
            try:
                codes = auction_scan_codes(
                    load_watch_pool(args.asof),
                    full_universe=args.full_universe,
                )
            except DataSourceError as e:
                print(json.dumps({"status": "insufficient_data", "error": str(e)}, ensure_ascii=False))
                sys.exit(1)
        try:
            state = append_snapshot(codes, args.asof, full_universe=args.full_universe)
        except DataSourceError as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
            sys.exit(1)
        counts = {c: len(s) for c, s in state.get("series", {}).items()}
        print(json.dumps({"ok": True, "asof": args.asof, "snapshot_counts": counts}, ensure_ascii=False))
        return
    elif args.finalize:
        try:
            result = finalize(args.asof, shortlist_limit=args.shortlist_limit)
        except DataSourceError as e:
            print(json.dumps({"status": "insufficient_data", "error": str(e)}, ensure_ascii=False))
            sys.exit(1)
    else:
        parser.error("需指定 --snapshot / --finalize / --once / --example 之一")

    if args.json:
        print(json.dumps(json_report(result), ensure_ascii=False))
    else:
        print(f"## 集合竞价因子 | {result['asof']}")
        for f in result["factors"]:
            if f.get("error"):
                print(f"- {f['name']}({f['code']}): {f['error']}")
                continue
            print(f"- {f['name']}({f['code']}): gap={f['auction_gap_pct']}% "
                  f"状态={f['board_status']} 委比={f['auction_bid_ask_ratio']} "
                  f"净委买增={f['auction_net_bid_delta']} 封单/流通={f['seal_amount_ratio_pct']}%")


if __name__ == "__main__":
    main()
