#!/usr/bin/env python3
"""
集合竞价采集器 — 9:15-9:25 真竞价量价因子
===========================================
竞价量价主数据源为 easy_tdx MAC ``0x123D``，五档盘口由腾讯实时快照补充。
两类字段按来源融合：腾讯成交量绝不替代真实竞价量或昨日量。

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

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
from a_stock_http import DataSourceError  # noqa: E402
from auction_data_provider import fetch_real_auction_snapshots  # noqa: E402
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
from discovery_recall import (  # noqa: E402
    _code_set,
    is_recall_target_event,
)

AUCTION_OPEN_FREEZE = "09:20"  # 9:20 后委托不可撤单，9:20→9:25 委买净增 = 无撤单窗口真实意图
QUOTE_BATCH_SIZE = 80
MAX_POOL_AGE_DAYS = 4
AUCTION_WINDOW_START_MINUTE = 9 * 60 + 15
AUCTION_WINDOW_END_MINUTE = 9 * 60 + 25
AUCTION_EXPECTED_TIMEPOINTS = AUCTION_WINDOW_END_MINUTE - AUCTION_WINDOW_START_MINUTE + 1
QUALITY_MIN_NONZERO_RATE = 0.5
QUALITY_MAX_MIRROR_RATE = 0.5
QUALITY_MIN_TIME_COVERAGE = 0.5
PREFREEZE_START_MINUTE = 9 * 60 + 15
PREFREEZE_END_MINUTE = 9 * 60 + 19
PREFREEZE_MIN_REAL_SAMPLES = 2
ORDER_BOOK_UNSUPPORTED_PROVIDERS = frozenset({"easy_tdx_mac_0x123d"})
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


def _has_complete_five_level_book(snap: Mapping[str, Any]) -> bool:
    """Require all five bid and ask levels; partial books are not evidence."""
    def complete(levels: Any) -> bool:
        return (
            isinstance(levels, (list, tuple))
            and len(levels) >= 5
            and all(
                isinstance(level, (list, tuple))
                and len(level) >= 2
                and float(level[0] or 0) > 0
                and float(level[1] or 0) > 0
                for level in levels[:5]
            )
        )
    return complete(snap.get("bids")) and complete(snap.get("asks"))


def _is_real_book_observation(snap: Mapping[str, Any]) -> bool:
    provenance = snap.get("book_observation_provenance")
    return (
        snap.get("book_is_imputed") is False
        and isinstance(provenance, Mapping)
        and provenance.get("observation_kind") == "observed"
    )


def _shadow_provenance() -> Dict[str, Any]:
    return {
        "method": "pre_freeze_bid_volume_change_proxy_v1",
        "observation_window": "09:15-09:19",
        "observation_kind": "real_observed_five_level_books_only",
        "interpretation": "委买量变化代理，不等于可证明撤单",
        "excluded_imputed_books": True,
    }


def _real_prefreeze_snapshots(snapshots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    eligible = [
        snap for snap in snapshots
        if (slot := _minute_slot(snap.get("t"))) is not None
        and PREFREEZE_START_MINUTE <= slot <= PREFREEZE_END_MINUTE
        and _has_complete_five_level_book(snap)
        and _is_real_book_observation(snap)
    ]
    by_minute = {}
    for snap in eligible:
        by_minute.setdefault(_minute_slot(snap.get("t")), snap)
    return [by_minute[key] for key in sorted(by_minute)]


def _real_prefreeze_book_count(snapshots: List[Dict[str, Any]]) -> int:
    return len(_real_prefreeze_snapshots(snapshots))


def _unavailable_shadow(reason: str, samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "status": "unavailable", "value_pct": None, "bid_volume_delta": None,
        "real_sample_count": len(samples),
        "real_minutes": [_minute_slot(snap.get("t")) for snap in samples],
        "reason": reason, "provenance": _shadow_provenance(),
    }


def _pre_freeze_cancel_shadow(snapshots: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Shadow proxy for bid-volume change before 09:20, never proof of cancels."""
    ordered = _real_prefreeze_snapshots(snapshots)
    if len(ordered) < PREFREEZE_MIN_REAL_SAMPLES:
        return _unavailable_shadow("真实五档盘口样本不足", ordered)
    first = _snapshot_bid_vol(ordered[0])
    last = _snapshot_bid_vol(ordered[-1])
    if first <= 0:
        return _unavailable_shadow("首个真实五档委买量无效", ordered)
    delta = round(last - first, 1)
    return {
        "status": "shadow",
        "value_pct": round((first - last) / first * 100, 2),
        "bid_volume_delta": delta,
        "real_sample_count": len(ordered),
        "real_minutes": [_minute_slot(snap.get("t")) for snap in ordered],
        "reason": None,
        "provenance": _shadow_provenance(),
    }


def _quality_for_snapshots(
    snapshots: List[Dict[str, Any]],
    *,
    require_window_coverage: bool = True,
) -> AuctionQuality:
    total = len(snapshots)
    nonzero_count = sum(1 for snap in snapshots if (snap.get("volume") or 0) > 0)
    book_count = sum(1 for snap in snapshots if _has_valid_book(snap))
    mirror_count = sum(1 for snap in snapshots if _is_mirrored_book(snap))
    providers = {
        str(snap.get("provider") or "").strip()
        for snap in snapshots
        if str(snap.get("provider") or "").strip()
    }
    book_contract_present = any(
        "book_status" in snap or "book_provider" in snap for snap in snapshots
    )
    book_coverage_required = book_contract_present or not providers or not providers.issubset(
        ORDER_BOOK_UNSUPPORTED_PROVIDERS
    )
    valid_slots = [
        slot
        for snap in snapshots
        if (slot := _minute_slot(snap.get("t"))) is not None
    ]
    slots = set(valid_slots)
    nonzero_rate = nonzero_count / total if total else 0.0
    mirror_rate = mirror_count / book_count if book_count else None
    # Coverage is the fraction of supplied observations with a valid auction
    # timestamp. The window-relative value is also retained for operations;
    # a one-shot quote is valid data but is not mistaken for a full 09:15-09:25
    # series.
    time_coverage_rate = len(valid_slots) / total if total else 0.0
    window_coverage_rate = len(slots) / AUCTION_EXPECTED_TIMEPOINTS
    real_prefreeze_count = _real_prefreeze_book_count(snapshots)
    reasons: List[str] = []
    # L1 免费源不提供竞价成交量，量能非零率恒低是数据源局限而非信号缺失；
    # 不再作为质量门禁（2026-08-14，Master 确认仅 L1 数据，去掉该门槛）。
    # nonzero_rate 仍保留在输出字段中供审计，但不参与 unavailable 判定。
    if book_coverage_required and mirror_rate is None:
        reasons.append("五档盘口无有效覆盖")
    elif book_coverage_required and mirror_rate > QUALITY_MAX_MIRROR_RATE:
        reasons.append("五档委买卖镜像率超过质量门槛")
    if time_coverage_rate < QUALITY_MIN_TIME_COVERAGE:
        reasons.append("竞价时点覆盖率低于质量门槛")
    if require_window_coverage and window_coverage_rate < QUALITY_MIN_TIME_COVERAGE:
        reasons.append("竞价窗口覆盖率低于质量门槛")
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
        "book_coverage_required": book_coverage_required,
        "book_coverage_status": (
            "available" if book_count
            else "missing" if book_coverage_required
            else "not_supported"
        ),
        "timepoints_covered": len(slots),
        "expected_timepoints": AUCTION_EXPECTED_TIMEPOINTS,
        "window_coverage_required": require_window_coverage,
        "pre_freeze_cancel_window_shadow": {
            "status": "shadow_available" if real_prefreeze_count >= PREFREEZE_MIN_REAL_SAMPLES else "unavailable",
            "real_observed_five_level_sample_count": real_prefreeze_count,
            "window": "09:15-09:19",
            "reason": None if real_prefreeze_count >= PREFREEZE_MIN_REAL_SAMPLES else "真实五档盘口样本不足",
            "provenance": {
                "method": "pre_freeze_bid_volume_change_proxy_v1",
                "interpretation": "委买量变化代理，不等于可证明撤单",
                "excluded_imputed_books": True,
            },
        },
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


def _board_status(gap_pct: float, *, at_limit: bool, at_limit_down: bool, ask_vol: float) -> str:
    if at_limit_down:
        return "limit_down"         # 竞价跌停，买入无意义
    if at_limit and ask_vol == 0:
        return "yizi_seal"          # 竞价一字封死
    if at_limit:
        return "limit_up_with_ask"  # 竞价上板但有卖盘（T字/可撬）
    return "high_open" if gap_pct > 0 else "flat_or_low_open"


def _auction_reference_ratios(
    last: Dict[str, Any], *, price: float, volume: float, raw_volume: Any
) -> Dict[str, Any]:
    """竞价量额对昨日/涨停日的比值；缺基准就留 None，不拿 0 冒充。"""
    if raw_volume is None:
        return {
            "auction_volume_prev_day_ratio": None,
            "auction_amount_prev_day_ratio": None,
            "auction_volume_limitup_day_ratio": None,
        }
    return {
        "auction_volume_prev_day_ratio": (
            round(volume / float(last["prev_day_volume"]), 4)
            if last.get("prev_day_volume") else None
        ),
        "auction_amount_prev_day_ratio": (
            round((price * volume * 100) / float(last["prev_day_amount"]), 4)
            if last.get("prev_day_amount") else None
        ),
        "auction_volume_limitup_day_ratio": (
            round(volume / float(last["limitup_day_volume"]), 4)
            if last.get("limitup_day_volume") else None
        ),
    }


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

    board_status = _board_status(
        gap_pct, at_limit=at_limit, at_limit_down=at_limit_down, ask_vol=ask_vol,
    )

    quality_notes = _data_quality_notes(snapshots, volume)
    # easy_tdx 0x123D emits per-symbol state-change points, not a fixed
    # minute-by-minute grid. Window coverage is meaningful for the aggregate
    # collector run, but sparse yet valid single-symbol series must not fail it.
    quality = _quality_for_snapshots(snapshots, require_window_coverage=False)
    cancel_shadow = _pre_freeze_cancel_shadow(snapshots)
    quality["pre_freeze_cancel_window_shadow"] = {
        "status": cancel_shadow["status"],
        "real_sample_count": cancel_shadow["real_sample_count"],
        "real_minutes": cancel_shadow["real_minutes"],
        "provenance": cancel_shadow["provenance"],
        "reason": cancel_shadow["reason"],
    }

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
        # easy_tdx 0x123D raw units are shares; retain them for the execution
        # gate and audit while auction_volume remains lots for legacy formulas.
        "matched": last.get("matched"),
        "unmatched": last.get("unmatched"),
        "auction_matched_shares": last.get("matched"),
        "auction_unmatched_shares": last.get("unmatched"),
        "auction_matched_unit": last.get("matched_unit", "share"),
        "auction_unmatched_unit": last.get("unmatched_unit", "share"),
        "auction_volume_unit": last.get("volume_unit", "lot"),
        "prev_day_volume": last.get("prev_day_volume"),
        "auction_amount": round(price * volume * 100, 0) if raw_volume is not None else None,
        "prev_day_amount": last.get("prev_day_amount"),
        "limitup_day_volume": last.get("limitup_day_volume"),
        **_auction_reference_ratios(last, price=price, volume=volume, raw_volume=raw_volume),
        # Tencent's free snapshot does not expose these L2 fields. Preserve
        # their schema and fail closed instead of manufacturing a signal.
        "unmatched_volume_after_0920": last.get("unmatched_volume_after_0920"),
        "post_0920_unmatched_volume": last.get("post_0920_unmatched_volume"),
        "auction_unmatched_volume_after_0920": last.get("auction_unmatched_volume_after_0920"),
        "seal_stability": last.get("seal_stability"),
        "auction_seal_stability": last.get("auction_seal_stability"),
        "auction_bid_ask_ratio": round(bid_vol / ask_vol, 2) if ask_vol else None,
        "auction_net_bid_delta": _net_bid_delta(snapshots),
        # Shadow-only and intentionally not consumed by the 09:35 hard reject.
        "auction_cancel_window_shadow": cancel_shadow,
        "auction_book_provider": last.get("book_provider"),
        "auction_book_observation_provenance": last.get(
            "book_observation_provenance"
        ),
        "auction_book_is_imputed": last.get("book_is_imputed"),
        "auction_book_status": last.get("book_status"),
        "auction_book_failure_reason": last.get("book_failure_reason"),
        "board_status": board_status,
        "seal_amount_ratio_pct": seal_ratio_pct,
        "snapshots_used": len(snapshots),
        "is_yiziban": board_status == "yizi_seal",
        "is_limit_down": at_limit_down,
        "auction_data_quality": quality,
        "auction_data_quality_notes": quality_notes,
    }


def take_snapshot_with_failures(
    codes: List[str],
    *,
    asof: str | date | None = None,
    previous_day_metrics: Mapping[str, Mapping[str, Any]] | None = None,
    require_previous_day_metrics: bool = True,
    deadline_seconds: float | None = None,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, str]]:
    """抓取 easy_tdx 竞价量价、腾讯五档及昨日量能，同时带回逐股失败原因。

    provider 已经按 09:15-09:25 返回逐时点数据；失败标的不进 series，
    由 finalize 的空/缺量门禁统一 fail-closed；腾讯只补盘口，不补竞价量。
    失败原因必须一路带到 artifact —— 只报「采到几只」没法区分
    「池子本来就小」和「数据源挂了」。
    """
    kwargs: Dict[str, Any] = {"asof": asof or date.today()}
    if previous_day_metrics is not None:
        kwargs["previous_day_metrics"] = previous_day_metrics
    if not require_previous_day_metrics:
        kwargs["require_previous_day_metrics"] = False
    if deadline_seconds is not None:
        kwargs["deadline_seconds"] = deadline_seconds
    return fetch_real_auction_snapshots(codes, **kwargs)


def take_snapshot(
    codes: List[str],
    *,
    asof: str | date | None = None,
    previous_day_metrics: Mapping[str, Mapping[str, Any]] | None = None,
    require_previous_day_metrics: bool = True,
    deadline_seconds: float | None = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """只要 series 的调用方入口（``--once`` 与回放），失败原因不参与返回。"""
    snapshots, _failures = take_snapshot_with_failures(
        codes,
        asof=asof,
        previous_day_metrics=previous_day_metrics,
        require_previous_day_metrics=require_previous_day_metrics,
        deadline_seconds=deadline_seconds,
    )
    return snapshots


def _enrich_snapshot_names(
    quotes: Mapping[str, Any],
    names_by_code: Mapping[str, str],
) -> Dict[str, Any]:
    """Fill missing quote names without overwriting provider-supplied names."""
    enriched: Dict[str, Any] = {}
    for raw_code, raw_quote in quotes.items():
        rows = raw_quote if isinstance(raw_quote, list) else [raw_quote]
        updated_rows: List[Dict[str, Any]] = []
        for raw_row in rows:
            row = dict(raw_row) if isinstance(raw_row, Mapping) else {}
            code = candidate_pipeline.naked_code(row.get("code") or raw_code)
            if not str(row.get("name") or "").strip():
                row["name"] = names_by_code.get(code, "")
            updated_rows.append(row)
        enriched[raw_code] = updated_rows if isinstance(raw_quote, list) else updated_rows[0]
    return enriched


def _load_universe_names() -> Dict[str, str]:
    """Read the durable code→name cache used by candidate discovery."""
    payload = read_json(data_file("stock-triage", "universe_quotes_cache.json"), {})
    quotes = payload.get("quotes") if isinstance(payload, Mapping) else {}
    if not isinstance(quotes, Mapping):
        return {}
    return {
        candidate_pipeline.naked_code(code): str(item.get("name") or "")
        for code, item in quotes.items()
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    }


# 预算留 20% 给取数之后的活：state 合并、input snapshot 物化、artifact 落盘。
# 全池 500 只时这段不是零成本，抓满整个 timeout 会把它挤掉。
FETCH_BUDGET_RATIO = 0.8


def fetch_budget_seconds() -> float | None:
    """本次作业允许用于取数的墙钟预算。

    唯一真源是 manifest 的 ``run.timeout_seconds``，由 job runner 注入
    ``A_STOCK_JOB_TIMEOUT_SECONDS``；手工跑（无 runner）时不造预算，
    保持与改动前一致的无界行为。
    """
    raw = os.environ.get("A_STOCK_JOB_TIMEOUT_SECONDS")
    if not raw:
        return None
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return None
    return round(timeout * FETCH_BUDGET_RATIO, 3) if timeout > 0 else None


def summarize_snapshot_failures(
    failures: Mapping[str, str],
    *,
    sample: int = 3,
) -> Dict[str, Any]:
    """按原因聚合失败标的，artifact 里只留计数与少量样本码。

    全池 500 只同时失败是真实场景（easy_tdx 连不上、预算耗尽），
    逐股展开会直接把 artifact 的 max_output_chars 撑爆。
    """
    grouped: Dict[str, List[str]] = {}
    for code, reason in sorted(failures.items()):
        grouped.setdefault(str(reason), []).append(str(code))
    return {
        "total": len(failures),
        "by_reason": [
            {"reason": reason, "count": len(codes), "sample_codes": codes[:sample]}
            for reason, codes in sorted(
                grouped.items(), key=lambda item: (-len(item[1]), item[0])
            )
        ],
    }


def _previous_day_source_version(quotes: Mapping[str, Any]) -> str:
    """Return an auditable version for the previous-day volume source."""
    for quote in quotes.values():
        rows = quote if isinstance(quote, list) else [quote]
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            provenance = row.get("prev_day_provenance")
            if isinstance(provenance, Mapping) and provenance.get("provider") == "local_history":
                return str(
                    row.get("prev_day_source_version")
                    or provenance.get("source_version")
                    or "local-history-v1"
                )
            if row.get("prev_day_provider") == "local_history":
                return str(row.get("prev_day_source_version") or "local-history-v1")
    return "candidate-preopen-v1"


def _merge_auction_series(
    existing: Iterable[Mapping[str, Any]],
    incoming: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge cumulative provider responses without duplicating timepoints.

    ``easy_tdx`` returns the complete series seen so far on every poll.  A
    collector poll must therefore upsert by timestamp rather than append the
    response wholesale; otherwise the repeated early points make the quality
    coverage ratio collapse as the 09:25 close approaches.
    """
    by_time: Dict[str, Dict[str, Any]] = {}
    for row in existing:
        if isinstance(row, Mapping) and row.get("t"):
            by_time[str(row["t"])] = dict(row)
    for row in incoming:
        if isinstance(row, Mapping) and row.get("t"):
            timestamp = str(row["t"])
            # The 09:24 full-market pass intentionally omits historical
            # fields.  Do not let that lightweight row erase fields already
            # captured by the executable 09:15-09:23 pass.
            previous = dict(by_time.get(timestamp) or {})
            merged = dict(previous)
            merged.update(dict(row))
            # A transient Tencent failure must not erase a valid book already
            # captured for the same auction point. Keep it as explicitly stale
            # evidence while retaining the latest failure reason for audit.
            if _has_valid_book(previous) and not _has_valid_book(merged):
                merged["bids"] = previous.get("bids")
                merged["asks"] = previous.get("asks")
                merged["book_provider"] = previous.get("book_provider")
                merged["book_provenance"] = previous.get("book_provenance")
                merged["book_observation_provenance"] = previous.get(
                    "book_observation_provenance"
                )
                merged["book_is_imputed"] = previous.get("book_is_imputed", False)
                merged["book_status"] = "stale_last_good"
            for key, value in previous.items():
                if merged.get(key) is None and value is not None:
                    merged[key] = value
            by_time[timestamp] = merged

    rows = [by_time[key] for key in sorted(by_time)]
    # A later lightweight quote may be a new timestamp, so same-timestamp
    # merging is not enough.  These fields are stable for the whole auction
    # window; carry them onto the final quote used by factor calculation.
    stable_fields = ("prev_close", "prev_day_volume", "prev_day_amount")
    stable_values = {
        key: next((row.get(key) for row in rows if row.get(key) is not None), None)
        for key in stable_fields
    }
    for row in rows:
        for key, value in stable_values.items():
            if row.get(key) is None and value is not None:
                row[key] = value
    last_good_book = next(
        (row for row in reversed(rows) if _has_valid_book(row)),
        None,
    )
    if last_good_book is not None:
        for row in rows:
            if _has_valid_book(row):
                continue
            row["bids"] = last_good_book.get("bids")
            row["asks"] = last_good_book.get("asks")
            row["book_provider"] = last_good_book.get("book_provider")
            row["book_provenance"] = last_good_book.get("book_provenance")
            original_provenance = last_good_book.get("book_observation_provenance")
            row["book_observation_provenance"] = {
                "observation_kind": "imputed",
                "imputed_from_timestamp": last_good_book.get("t"),
                "original_observation_provenance": original_provenance,
            }
            row["book_is_imputed"] = True
            # A copied book is never current-time evidence, regardless of
            # whether the missing row was explicitly reported unavailable.
            row["book_status"] = "stale_last_good"
    return rows








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
        if isinstance(raw_quote, list):
            # The real provider returns a series; full-market recall stores
            # only the latest point because it is intelligence-only and does
            # not feed the executable shortlist.
            quote = dict(raw_quote[-1]) if raw_quote else {}
            quote["auction_series_count"] = len(raw_quote)
        else:
            quote = dict(raw_quote)
        code = candidate_pipeline.naked_code(quote.get("code") or raw_code)
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


def build_new_strong_research_pool(
    factors: Iterable[Mapping[str, Any]],
    series: Mapping[str, Iterable[Mapping[str, Any]]],
    pool: Mapping[str, Any],
    asof: str,
) -> List[Dict[str, Any]]:
    """Build a descriptive same-day recall pool without entering ranking.

    This is intentionally a sidecar: stale/failed candidate-preopen may not
    be allowed to hide real same-day limit-up or high-open observations, but
    these rows must never be appended to ``research_candidates`` (the auction
    ranking input) or ``execution_candidates``.
    """
    prefilter = _code_set(pool.get("prefilter_codes") or [])
    rows: List[Dict[str, Any]] = []
    for factor in factors:
        code = candidate_pipeline.naked_code(factor.get("code"))
        if not code or code in prefilter:
            continue
        observations = list(series.get(factor.get("code"), ()))
        target = bool(factor.get("recall_target_event")) or any(
            is_recall_target_event({**dict(row), "code": code}) for row in observations
        )
        if not target:
            continue
        row = dict(factor)
        row.update({
            "code": code,
            "research_only": True,
            "execution_eligible": False,
            "execution_action": "none",
            "participation_scope": "today_new_strong_research_only",
            "enhancement_reason": (
                "今日真实竞价数据满足涨停/大幅高开增强观察条件，候选池外补召回"
            ),
            "evidence_provenance": {
                "provider": "easy_tdx_mac_0x123d",
                "dataset": "auction_quote_inputs_v1",
                "date": asof,
                "coverage": "full_market_snapshot",
            },
        })
        rows.append(row)
    return sorted(rows, key=lambda row: (-float(row.get("auction_gap_pct") or 0), row["code"]))










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
    if not (
        pool.get("candidates")
        or pool.get("research_candidates")
        or pool.get("execution_candidates")
        or pool.get("auction_scan_universe")
        or pool.get("auction_scan_codes")
    ):
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


def watch_pool_staleness(pool: Mapping[str, Any], event_asof: str) -> Dict[str, Any]:
    """观察池相对当日的新鲜度画像。

    ``candidate_pool_latest.json`` 是一个 latest 文件：candidate-preopen（08:30）
    挂掉时它会停在上一交易日，而 ``load_watch_pool`` 本来就容忍
    ``MAX_POOL_AGE_DAYS`` 天 —— 也就是说隔夜池此前会被当成正常池静默使用。
    抓隔夜池的竞价是可以的（竞价数据是今天的真实数据），但它不能进执行面：
    今天新出现的标的一个都不在池子里。所以这里把 stale 显式记下来，
    由 ``finalize`` 转成 ``research_only``。
    """
    pool_asof = str((pool or {}).get("asof") or "").strip()
    if not pool_asof:
        return {
            "stale": True,
            "pool_asof": None,
            "age_days": None,
            "reason": "观察池不可用（candidate-preopen 无产出）",
        }
    try:
        age = (
            datetime.fromisoformat(event_asof).date()
            - datetime.fromisoformat(pool_asof).date()
        ).days
    except ValueError:
        return {
            "stale": True,
            "pool_asof": pool_asof,
            "age_days": None,
            "reason": f"观察池日期无法解析: {pool_asof}",
        }
    if age <= 0:
        return {"stale": False, "pool_asof": pool_asof, "age_days": age, "reason": None}
    return {
        "stale": True,
        "pool_asof": pool_asof,
        "age_days": age,
        "reason": (
            f"使用隔夜观察池（source={pool_asof}，落后 {age} 天）："
            "竞价为当日真实数据，但候选集合不含今日新标的，仅作研究观测"
        ),
    }


def watch_pool_codes(pool: Mapping[str, Any]) -> List[str]:
    """issue #260 B.1：D0 已批准的 execution_candidates 与 local_theme_candidates
    的全部必要成员都要获得 09:15-09:25 因子，否则 09:25 二次确认永远没有新鲜
    竞价证据可用。普通 research_candidates 不在这条路径——它们只属于研究扫描，
    不能因此获得局部准入。"""
    candidates = (
        pool.get("execution_candidates")
        if "execution_candidates" in pool
        else pool.get("candidates", [])
    )
    codes = [
        candidate_pipeline.market_code(item.get("code") or item.get("market_code"))
        for item in candidates or []
        if item.get("code") or item.get("market_code")
    ]
    local_theme_codes = [
        candidate_pipeline.market_code(item.get("code") or item.get("market_code"))
        for item in pool.get("local_theme_candidates") or []
        if item.get("code") or item.get("market_code")
    ]
    return list(dict.fromkeys([*codes, *local_theme_codes]))


def auction_scan_codes(
    pool: Mapping[str, Any],
    *,
    full_universe: bool,
    bounded_universe: bool = False,
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
        pool.get("full_market_codes")
        or pool.get("auction_scan_universe")
        or pool.get("auction_scan_codes")
        if full_universe
        else None
    )
    if bounded_universe:
        # The 09:24 enhancement is deliberately bounded. ``full_market_codes``
        # is a recall universe, not a pre-open SLA input.
        source = pool.get("auction_scan_codes") or pool.get("auction_scan_universe")
    if not source:
        codes = watch_pool_codes(pool)
        if codes:
            return codes
        source = pool.get("auction_scan_universe") or pool.get("auction_scan_codes")
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
    bounded_universe: bool = False,
) -> Dict[str, Any]:
    """事务式把一次快照追加到当日状态文件（单锁 read-modify-write）。"""
    try:
        pool = load_watch_pool(asof)
    except DataSourceError:
        # Unit/replay callers may provide their own snapshot adapter. Production
        # cron still has the candidate-preopen pool and uses it when available.
        pool = {}
    # Candidate contract v2 keeps research and execution views separately.
    # Both can carry the already-fetched daily volume; using only
    # research_candidates made the 09:15 batch refetch history for every name
    # whenever that view omitted prev_close, exhausting the fetch budget.
    metric_candidates = [
        item
        for key in ("execution_candidates", "research_candidates", "candidates")
        for item in (pool.get(key) or [])
        if isinstance(item, Mapping)
    ]
    previous_day_metrics: Dict[str, Dict[str, Any]] = {}
    for item in metric_candidates:
        code = candidate_pipeline.naked_code(item.get("code") or item.get("market_code"))
        volume = item.get("volume")
        if not code or not volume:
            continue
        # Prefer the first valid row (execution view normally has the freshest
        # quote), but retain the previous close so the provider can trust the
        # supplied volume without another historical request.
        previous_day_metrics.setdefault(code, {
            "prev_day_volume": volume,
            "prev_day_amount": item.get("amount"),
            "prev_close": item.get("prev_close") or item.get("close"),
            "prev_day_date": item.get("prev_day_date") or asof,
            "prev_day_provider": item.get("quote_source") or item.get("provider") or "candidate-preopen",
            "prev_day_provenance": {
                "provider": item.get("quote_source") or item.get("provider") or "candidate-preopen",
                "dataset": "candidate_watch_pool_v1",
                "date": item.get("prev_day_date") or asof,
            },
        })
    pool_stale = watch_pool_staleness(pool, asof)
    budget = fetch_budget_seconds()
    if full_universe:
        # The 09:24 full-market pass is intelligence-only. It must not fan out
        # one historical K-line request per symbol or block the executable
        # 500-name auction pool when the historical source is slow.
        raw_quotes, fetch_failures = take_snapshot_with_failures(
            codes,
            asof=asof,
            require_previous_day_metrics=False,
            deadline_seconds=budget,
        )
    elif previous_day_metrics:
        raw_quotes, fetch_failures = take_snapshot_with_failures(
            codes,
            asof=asof,
            previous_day_metrics=previous_day_metrics,
            deadline_seconds=budget,
        )
    else:
        raw_quotes, fetch_failures = take_snapshot_with_failures(
            codes, asof=asof, deadline_seconds=budget
        )
    failure_summary = summarize_snapshot_failures(fetch_failures)
    # easy_tdx may omit the display name on the lightweight/full-market pass.
    # Enrich before materialising the immutable input snapshot so every
    # downstream consumer (finalize, brief, intraday monitor) sees the same
    # identity data.
    raw_quotes = _enrich_snapshot_names(raw_quotes, _load_universe_names())
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
        source_versions={
            "auction": "easy_tdx_mac_0x123d",
            "order_book": "tencent_quote_v1",
            "previous_day_volume": _previous_day_source_version(raw_quotes),
        },
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
            if isinstance(q, list):
                series[code] = _merge_auction_series(series.get(code, []), q)
            else:
                # Compatibility for old test/replay adapters returning one
                # Tencent-style snapshot per code.
                series[code] = _merge_auction_series(series.get(code, []), [q])
        if full_universe:
            state["full_market_snapshot_at"] = datetime.now().isoformat(timespec="seconds")
            state["full_market_snapshot_count"] = len(quotes)
        if bounded_universe:
            state["bounded_auction_snapshot_at"] = datetime.now().isoformat(timespec="seconds")
            state["bounded_auction_snapshot_requested_count"] = len(codes)
            state["bounded_auction_snapshot_count"] = len(quotes)
            state["bounded_auction_snapshot_scope"] = "auction_scan_codes"
        # 只保留本次采集的失败画像：它回答的是「这一分钟的窗口发生了什么」，
        # 跨快照累积会把早已恢复的失败一直挂在 artifact 上。
        state["snapshot_failures"] = failure_summary
        # 池子新鲜度是「今天这一轮」的属性，同样只留最新一次。
        state["pool_stale"] = pool_stale
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
    research_candidates = list(pool.get("research_candidates") or [])
    execution_candidates = list(
        pool.get("execution_candidates")
        if "execution_candidates" in pool
        else pool.get("candidates") or []
    )
    auction_scan_count = int(
        (pool.get("counts") or {}).get("auction_scan")
        or pool.get("auction_scan_count")
        or len(pool.get("auction_scan_universe") or pool.get("auction_scan_codes") or [])
    )
    market_intelligence = optional_market_intelligence_status()
    result.update({
        "schema": "auction_finalize_v2",
        "asof": asof,
        "status": "degraded",
        "outcome_status": "failed_data",
        "reason_code": "auction_collection_empty",
        "collection_status": "empty",
        "research_only": True,
        "degraded_reasons": [reason],
        "source_asof": pool.get("asof"),
        "input_count": len(research_candidates),
        "research_count": len(research_candidates),
        "execution_input_count": len(execution_candidates),
        "execution_count": 0,
        "auction_scan_count": auction_scan_count,
        "factor_count": 0,
        "shortlist": [],
        "research_candidates": [],
        "execution_candidates": [],
        "local_theme_candidates": [],
        "conditional_candidates": [],
        "local_theme_count": 0,
        "conditional_count": 0,
        "shortlist_count": 0,
        "rejected": [],
        "preopen_decisions": [],
        "decision_count": 0,
        "discipline_state": None,
        "rejection_reason_counts": {},
        "gate": dict(pool.get("gate") or {}),
        "market_intelligence": market_intelligence,
        "market_intelligence_degraded": bool(market_intelligence.get("degraded")),
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


def optional_market_intelligence_status() -> Dict[str, Any]:
    """Describe the optional full-market enhancement without gating finalize."""
    raw = os.environ.get("HERMES_CONTEXT_FROM") or ""
    if not raw:
        return {"status": "not_observed", "degraded": False, "reasons": []}
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "status": "degraded",
            "degraded": True,
            "reasons": ["optional_dependency_context_invalid"],
        }
    for entry in entries if isinstance(entries, list) else []:
        if entry.get("job_id") != "auction-market-snapshot":
            continue
        status = str(entry.get("status") or "missing")
        reasons = [str(item) for item in entry.get("reasons") or []]
        degraded = status != "ok" or bool(reasons)
        return {
            "status": "market_intelligence_degraded" if degraded else "ok",
            "degraded": degraded,
            "upstream_status": status,
            "reasons": reasons or ([f"status_{status}"] if degraded else []),
        }
    return {
        "status": "market_intelligence_degraded",
        "degraded": True,
        "upstream_status": "missing",
        "reasons": ["auction_market_snapshot_missing"],
    }


def finalize(asof: str, shortlist_limit: int = DEFAULT_SHORTLIST_LIMIT) -> Dict[str, Any]:
    state = read_json(_state_path(asof), default={"series": {}})
    result = _build_result(state.get("series", {}), asof)
    # 采集期的失败画像必须跟到收口报告：09:26 看到 shortlist 很短时，
    # 「池子本来就小」和「数据源挂了/预算耗尽」是两种完全不同的运维动作。
    result["snapshot_failures"] = state.get("snapshot_failures")
    result["pool_stale"] = state.get("pool_stale")
    try:
        pool_for_recall = load_watch_pool(asof)
    except DataSourceError:
        pool_for_recall = {}
    result["new_strong_research_pool"] = build_new_strong_research_pool(
        result.get("factors") or [], state.get("series") or {}, pool_for_recall, asof,
    )
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
    result["market_intelligence"] = optional_market_intelligence_status()
    result["market_intelligence_degraded"] = bool(
        result["market_intelligence"].get("degraded")
    )
    ranking_quality = result.get("auction_quality", {}).get("ranking_summary", {})
    critical_volume_missing = int(ranking_quality.get("critical_volume_missing_count") or 0)
    global_quality_status = _quality_status(result.get("auction_quality"))
    result["status"] = "degraded" if global_quality_status == "unavailable" else "ready"
    if global_quality_status == "unavailable":
        result["collection_status"] = "insufficient_quality"
        result["degraded_reasons"] = ["竞价整体质量不足，拒绝输出可执行结论"]
    # 空短名单（弱市门禁清零候选池）时不得伪装成可执行结论：
    # research_only=True + decision_count=0 让下游明确区分"无机会"与"无观测"。
    result["research_only"] = len(result["shortlist"]) == 0
    # 隔夜观察池：竞价是今天的真实数据，可以观测，但候选集合不含今日新标的，
    # 不能进执行面。open_confirmation 对 research_only 有清零信号的 fail-closed
    # 安全网，这里把开关拨到位并写明原因。
    stale = result.get("pool_stale") or {}
    if stale.get("stale"):
        result["research_only"] = True
        result["status"] = "degraded"
        reasons = list(result.get("degraded_reasons") or [])
        reason = str(stale.get("reason") or "观察池已过期")
        if reason not in reasons:
            reasons.append(reason)
        result["degraded_reasons"] = reasons
    if result["status"] == "degraded":
        result["outcome_status"] = "failed_data"
        if stale.get("stale"):
            result["reason_code"] = "stale_candidate_pool"
        elif global_quality_status == "unavailable":
            result["reason_code"] = "auction_quality_insufficient"
        else:
            result["reason_code"] = "auction_collection_degraded"
    elif result["shortlist"]:
        result["outcome_status"] = "ok_with_candidates"
        result["reason_code"] = "execution_candidates_available"
    elif result.get("research_candidates"):
        result["outcome_status"] = "ok_research_only"
        result["reason_code"] = str(
            (result.get("gate") or {}).get("status") or "execution_gate_filtered_all"
        )
    else:
        result["outcome_status"] = "ok_no_actionable_candidates"
        result["reason_code"] = "no_research_or_execution_candidates"
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
    names_by_code = _load_universe_names()
    computed = [
        compute_auction_factors(
            snaps,
            code,
            (snaps[-1].get("name") if snaps else "")
            or names_by_code.get(candidate_pipeline.naked_code(code), ""),
        )
        for code, snaps in series.items()
    ]
    factor_failures = [factor for factor in computed if factor.get("error")]
    factors = [factor for factor in computed if not factor.get("error")]
    quality = _quality_for_snapshots([
        snap for snapshots in series.values() for snap in snapshots
    ])
    quality["factor_summary"] = {
        "observation_count": len(computed),
        "factor_count": len(factors),
        "failure_count": len(factor_failures),
        "unavailable_count": sum(
            _quality_status(factor.get("auction_data_quality")) == "unavailable"
            for factor in factors
        ),
    }
    return {
        "schema": "auction_factors_v1",
        "asof": asof,
        "generated_at": datetime.now().isoformat(),
        "note": (
            "easy_tdx 0x123D 真竞价因子（该接口不提供五档盘口）；"
            "auction_score 为 0-100 启发式排序分，"
            "不是涨停概率或收益概率；量能关键字段缺失时拒绝进入可交易短名单；"
            "撤单率类信号需 L2；阈值须经 chanlun-backtest 验证后方可实盘"
        ),
        "factors": factors,
        "factor_failures": factor_failures,
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
        "outcome_status": result.get("outcome_status"),
        "reason_code": result.get("reason_code"),
        "asof": result.get("asof"),
        "generated_at": result.get("generated_at"),
        "source_asof": result.get("source_asof"),
        # 曾硬编码 False，使降级报告自相矛盾（status=degraded 却 research_only=False）
        "research_only": bool(result.get("research_only", False)),
        "degraded_reasons": list(result.get("degraded_reasons") or []),
        "collection_status": result.get("collection_status"),
        "snapshot_failures": result.get("snapshot_failures"),
        "pool_stale": result.get("pool_stale"),
        "market_intelligence": result.get("market_intelligence"),
        "market_intelligence_degraded": bool(result.get("market_intelligence_degraded")),
        "auction_quality": result.get("auction_quality") or result.get("auction_quality_report"),
        "auction_quality_report": result.get("auction_quality_report"),
        "pre_freeze_cancel_window_shadow": (
            (result.get("auction_quality") or {}).get(
                "pre_freeze_cancel_window_shadow"
            )
        ),
        "score_semantics": result.get(
            "score_semantics", "heuristic_rank_score_not_probability"
        ),
        "score_label": result.get(
            "score_label", "竞价启发式排序分（0-100，非涨停概率/收益概率）"
        ),
        "score_is_probability": False,
        "input_count": result.get("input_count"),
        "research_count": result.get("research_count", 0),
        "execution_input_count": result.get("execution_input_count", 0),
        "execution_count": result.get("execution_count", len(result.get("shortlist") or [])),
        "auction_scan_count": result.get("auction_scan_count", 0),
        "shortlist_count": result.get("shortlist_count", len(result.get("shortlist") or [])),
        "decision_count": result.get("decision_count", len(decisions)),
        "discipline_state": result.get("discipline_state"),
        "rejection_reason_counts": dict(result.get("rejection_reason_counts") or {}),
        "gate": dict(result.get("gate") or {}),
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


def _run_snapshot_cli(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    codes = [c.strip() for c in args.codes.split(",")] if args.codes else []
    if not codes:
        try:
            codes = auction_scan_codes(
                load_watch_pool(args.asof),
                full_universe=args.full_universe,
                bounded_universe=args.bounded_universe,
            )
        except DataSourceError as e:
            print(json.dumps({"status": "insufficient_data", "error": str(e)}, ensure_ascii=False))
            sys.exit(1)
    try:
        state = append_snapshot(
            codes,
            args.asof,
            full_universe=args.full_universe,
            bounded_universe=args.bounded_universe,
        )
    except DataSourceError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps({
        "ok": True,
        "asof": args.asof,
        "snapshot_counts": {c: len(s) for c, s in state.get("series", {}).items()},
        # 「采到几只」必须和「剩下的怎么了」一起出现，否则窗口出问题时
        # 分不清是池子小还是数据源挂了。
        "snapshot_failures": state.get("snapshot_failures"),
        "snapshot_scope": state.get("bounded_auction_snapshot_scope")
        or ("full_market" if args.full_universe else "auction_pool"),
        "snapshot_requested_count": state.get("bounded_auction_snapshot_requested_count"),
        "snapshot_completed_count": state.get("bounded_auction_snapshot_count"),
    }, ensure_ascii=False))


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
    parser.add_argument(
        "--bounded-universe",
        action="store_true",
        help="09:24增强扫描仅使用auction_scan_codes预备池，不扫描full_market_codes",
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
            series = take_snapshot(codes, asof=args.asof)
        except DataSourceError as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
            sys.exit(1)
        result = _build_result(series, args.asof)
    elif args.snapshot:
        _run_snapshot_cli(args, parser)
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
