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
DEFAULT_SHORTLIST_LIMIT = int(
    load_registered("candidate_selection")["pipeline"]["auction_shortlist_limit"]
)


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


def compute_auction_factors(snapshots: List[Dict[str, Any]], code: str, name: str = "") -> Dict[str, Any]:
    """从一只票的竞价快照序列算出 6 个真竞价因子（纯函数，不触网）。"""
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
    volume = last.get("volume") or 0.0           # 竞价累计成交量（手）
    market_cap_yi = last.get("market_cap")       # 流通市值（亿元）

    gap_pct = round((price - prev_close) / prev_close * 100, 2)
    limit_up = round_limit(prev_close, limit_pct(code, name), up=True)
    at_limit = price >= limit_up - 1e-6

    if at_limit and ask_vol == 0:
        board_status = "yizi_seal"          # 竞价一字封死
    elif at_limit:
        board_status = "limit_up_with_ask"  # 竞价上板但有卖盘（T字/可撬）
    elif gap_pct > 0:
        board_status = "high_open"
    else:
        board_status = "flat_or_low_open"

    seal_ratio_pct = None
    if at_limit and market_cap_yi:
        seal_ratio_pct = round(best_bid_vol * 100 * price / (market_cap_yi * 1e8) * 100, 3)

    return {
        "code": code,
        "name": name,
        "indicative_price": price,
        "prev_close": prev_close,
        "auction_gap_pct": gap_pct,
        "auction_volume": round(volume, 1),
        "auction_amount": round(price * volume * 100, 0),
        "auction_bid_ask_ratio": round(bid_vol / ask_vol, 2) if ask_vol else None,
        "auction_net_bid_delta": _net_bid_delta(snapshots),
        "board_status": board_status,
        "seal_amount_ratio_pct": seal_ratio_pct,
        "snapshots_used": len(snapshots),
        "is_yiziban": board_status == "yizi_seal",
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
    has_candidates = bool((pool or {}).get("candidates"))
    has_scan_universe = bool((pool or {}).get("auction_scan_codes"))
    if not isinstance(pool, dict) or pool.get("status") != "ready" or not (has_candidates or has_scan_universe):
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
    """Return deep-pool codes or the full eligible one-shot scan universe."""
    source = pool.get("auction_scan_codes") if full_universe or not pool.get("candidates") else None
    if not source:
        return watch_pool_codes(pool)
    return list(dict.fromkeys(
        candidate_pipeline.market_code(code)
        for code in source
        if code
    ))


def append_snapshot(codes: List[str], asof: str) -> Dict[str, Any]:
    """事务式把一次快照追加到当日状态文件（单锁 read-modify-write）。"""
    raw_quotes = take_snapshot(codes)
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


def finalize(asof: str, shortlist_limit: int = DEFAULT_SHORTLIST_LIMIT) -> Dict[str, Any]:
    state = read_json(_state_path(asof), default={"series": {}})
    result = _build_result(state.get("series", {}), asof)
    pool = load_watch_pool(asof)
    signal_ctx = read_signal_context(max_age_hours=8) or {}
    shortlist = candidate_pipeline.rank_auction_shortlist(
        pool,
        result["factors"],
        limit=shortlist_limit,
        signal_ctx=signal_ctx,
    )
    result.update(shortlist)
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
    return {
        "schema": "auction_factors_v1",
        "asof": asof,
        "generated_at": datetime.now().isoformat(),
        "note": "免费腾讯五档竞价因子；撤单率类信号需 L2；阈值须经 chanlun-backtest 验证后方可实盘",
        "factors": factors,
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
        "research_only": False,
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
            state = append_snapshot(codes, args.asof)
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
