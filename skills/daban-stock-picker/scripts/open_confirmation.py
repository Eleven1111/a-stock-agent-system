#!/usr/bin/env python3
"""
09:35 open confirmation for the limit-up candidate workflow.

Reads the 09:25 auction factor state, fetches current Tencent quotes, and emits
a compact JSON decision surface for stock-triage. It does not place orders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Sequence

SCRIPT_DIR = os.path.dirname(__file__)
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "..", "common"))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "..", "stock-triage", "scripts"))

from a_stock_http import DataSourceError  # noqa: E402
from market_adapters import fetch_tencent_snapshot  # noqa: E402
from announcement_risk import scan_many  # noqa: E402
from a_share_rules import add_trading_days  # noqa: E402
import candidate_lifecycle  # noqa: E402
import candidate_pipeline  # noqa: E402
import hot_money_selection  # noqa: E402
import stage_intelligence  # noqa: E402
from weak_market_delivery import assess_delivery_quality  # noqa: E402
from market_temperature import temperature_from_context  # noqa: E402
import monitor_registry  # noqa: E402
from paths import data_file  # noqa: E402
from config_registry import load_registered  # noqa: E402


DEFAULT_OPEN_LIMIT = int(
    load_registered("candidate_selection")["pipeline"]["open_confirmation_limit"]
)
from recommendation_quality import (  # noqa: E402
    build_execution_plan,
    build_quality_report,
    merge_market_intelligence,
)
from decision_policy import evaluate_decision  # noqa: E402
from market_snapshot import compact_ref, materialize_input_snapshot  # noqa: E402
from market_context import market_regime, read_market_context  # noqa: E402
from portfolio_policy import evaluate_candidate  # noqa: E402
from portfolio_research_history import record_open_confirmation  # noqa: E402
import recommendation_audit  # noqa: E402
from research_evidence import build_research_evidence  # noqa: E402
import signal_ledger  # noqa: E402
import strategy_registry  # noqa: E402
from signal_context import read_signal_context  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402
from tradeability import assess_tradeability  # noqa: E402

QUOTE_BATCH_SIZE = 80
POSITIVE_ACTIONS = {"buy", "add", "conditional_buy"}


def _naked_code(code: str) -> str:
    return code[2:] if code.startswith(("sh", "sz")) else code


def _recommendation_action(item: Mapping[str, Any]) -> str:
    """Preserve research intent in the ledger without reviving other blocks."""
    decision = str(item.get("decision") or "watch").lower()
    if decision == "avoid":
        return "avoid"
    if decision in POSITIVE_ACTIONS:
        return decision
    policy = item.get("policy_decision") or {}
    requested = str(policy.get("requested_action") or "hold").lower()
    reasons = {str(reason) for reason in policy.get("reasons") or []}
    if decision == "watch" and requested in POSITIVE_ACTIONS and reasons == {"strategy_unverified"}:
        return requested
    return "hold"


def _enrich_decision(
    candidate: Mapping[str, Any],
    announcements: Sequence[Mapping[str, Any]] | None,
    asof: str,
) -> Dict[str, Any]:
    item = dict(candidate)
    provisional_quality = {
        "status": "passed",
        "execution_constraints": {},
    }
    provisional = build_execution_plan(item, provisional_quality, asof=asof, stage="open")
    recommendation = {
        **item,
        "action": "buy" if item.get("action") == "trend_watch" else "hold",
        "entry_price": item.get("price"),
        "price_range": provisional["entry_range"],
        "stop_price": provisional["stop_price"],
        "target_price": provisional["target_price"],
        "horizon": provisional["horizon"],
        "grade": "A" if float(item.get("open_score") or 0) >= 80 else "B",
        "confidence": "medium",
        "position_pct": provisional["position_pct"] or 4.0,
    }
    quality = build_quality_report(recommendation, announcements, asof=asof)
    plan = build_execution_plan(item, quality, asof=asof, stage="open")
    item["announcements"] = list(announcements) if announcements is not None else None
    item["quality_report"] = quality
    item["execution_plan"] = plan
    item["decision"] = plan["decision"]
    return item


def _apply_policy(
    item: Mapping[str, Any],
    asof: str | None = None,
    portfolio: Mapping[str, Any] | None = None,
    regime: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    result = dict(item)
    selected_by = result.get("open_selected_by") or {}
    selected_lane = "daban" if selected_by.get("daban") else "trend"
    strategy_id = hot_money_selection.selection_strategy_id(
        result,
        selected_lane,
    )
    lane = "daban" if strategy_id.startswith("daban") else "trend"
    evidence = build_research_evidence(
        _naked_code(str(result.get("code") or "")),
        strategy_id=strategy_id,
        asof=asof,
    )
    result["quality_report"] = merge_market_intelligence(
        result.get("quality_report") or {"status": "conditional"},
        evidence.get("market_intelligence"),
    )
    prior_chanlun = ((result.get("research_evidence") or {}).get("chanlun") or {})
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
        portfolio or {},
        result,
        float((result.get("execution_plan") or {}).get("position_pct") or 0),
    )
    selection_market = (result.get("selection_context") or {}).get("market_timing") or {}
    policy = evaluate_decision(
        requested_action=str((result.get("execution_plan") or {}).get("decision") or "watch"),
        quality_report=result.get("quality_report") or {"status": "conditional"},
        strategy_record=strategy_registry.live_record(strategy_id),
        market_regime=regime,
        portfolio_risk=portfolio_risk,
        research_evidence=evidence,
        strategy_lane=lane,
        market_crowding=selection_market,
    )
    result["strategy_id"] = strategy_id
    result["selection_context"] = hot_money_selection.advance_selection_context(
        result,
        window="09:35",
    )
    result["research_evidence"] = evidence
    result["portfolio_risk"] = portfolio_risk
    result["market_regime"] = dict(regime or {})
    result["policy_decision"] = policy
    if policy["decision"] in {"avoid", "watch"}:
        plan = dict(result.get("execution_plan") or {})
        plan["decision"] = policy["decision"]
        plan["position_pct"] = 0.0
        result["execution_plan"] = plan
        result["decision"] = policy["decision"]
    return result


def evaluate_open_confirmation(
    factor: Dict[str, Any],
    quote: Dict[str, Any],
    asof: str | None = None,
) -> Dict[str, Any]:
    code = factor.get("code", "")
    name = factor.get("name") or quote.get("name") or code
    tradeability = assess_tradeability(quote, _naked_code(code), name)
    change_pct = quote.get("change_pct")
    price = quote.get("price")

    action = "skip"
    reasons: List[str] = []
    if factor.get("error"):
        reasons.append(factor["error"])
    elif tradeability.get("tradeable") is False:
        action = "not_buyable"
        reasons.append(tradeability.get("reason", "不可成交"))
    elif factor.get("is_yiziban"):
        action = "not_buyable"
        reasons.append("09:25一字封死，高分也可能打不进")
    elif tradeability.get("status") == "limit_up":
        action = "queue_or_skip"
        reasons.append("已封涨停，仅可排队且不保证成交")
    elif change_pct is not None and 3.0 <= change_pct < 9.5:
        action = "trend_watch"
        reasons.append("符合趋势策略3%-10%中度上涨观察窗口")
    elif factor.get("board_status") in {"high_open", "limit_up_with_ask"}:
        action = "watch"
        reasons.append("竞价强但开盘未形成明确可执行信号")
    else:
        reasons.append("开盘确认不足")

    result = {
        "code": code,
        "name": name,
        "price": price,
        "prev_close": quote.get("prev_close"),
        "change_pct": change_pct,
        "auction_gap_pct": factor.get("auction_gap_pct"),
        "board_status": factor.get("board_status"),
        "action": action,
        "tradeability": tradeability,
        "reasons": reasons,
    }
    return _enrich_decision(
        result,
        factor.get("announcements") if "announcements" in factor else None,
        asof or date.today().isoformat(),
    )


def _shortlist_path(asof: str) -> str:
    return data_file("daban-stock-picker", f"auction_shortlist_{asof}.json")


def _confirmation_path(asof: str) -> str:
    return data_file("daban-stock-picker", f"open_confirmation_{asof}.json")


def _confirmation_latest_path() -> str:
    return data_file("daban-stock-picker", "open_confirmation_latest.json")


def load_shortlist(asof: str) -> Dict[str, Any]:
    shortlist = read_json(_shortlist_path(asof), {})
    if not isinstance(shortlist, dict) or shortlist.get("asof") != asof:
        raise DataSourceError("auction_shortlist", f"{asof} 竞价短名单缺失")
    return shortlist


def score_confirmations(
    shortlist: Sequence[Mapping[str, Any]],
    confirmations: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    prior = {
        candidate_pipeline.naked_code(item.get("code")): dict(item)
        for item in shortlist
    }
    action_quality = {"trend_watch": 1.0, "watch": 0.8}
    eligible = []
    for raw in confirmations:
        item = dict(raw)
        code = candidate_pipeline.naked_code(item.get("code"))
        if item.get("action") not in action_quality:
            continue
        merged = {**prior.get(code, {}), **item}
        change_pct = float(merged.get("change_pct") or 0.0)
        open_quality = max(0.0, 1.0 - abs(change_pct - 5.5) / 6.0)
        auction_score = float(merged.get("auction_score") or 0.0)
        merged["open_daban_score"] = round(
            0.65 * float(merged.get("auction_daban_score") or auction_score)
            + 20.0 * action_quality[item["action"]]
            + 15.0 * open_quality,
            2,
        )
        merged["open_trend_score"] = round(
            0.65 * float(merged.get("auction_trend_score") or auction_score)
            + 20.0 * action_quality[item["action"]]
            + 15.0 * open_quality,
            2,
        )
        merged["open_score"] = max(
            merged["open_daban_score"],
            merged["open_trend_score"],
        )
        eligible.append(merged)

    sector_groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in eligible:
        sector = str(item.get("sector") or "").strip()
        if sector:
            sector_groups.setdefault(sector, []).append(item)
    for members in sector_groups.values():
        ordered = sorted(
            members,
            key=lambda row: (-float(row.get("open_daban_score") or 0.0), str(row.get("code"))),
        )
        leader_score = float(ordered[0].get("open_daban_score") or 0.0) if ordered else 0.0
        for rank, item in enumerate(ordered, 1):
            item["open_sector_rank"] = rank
            item["open_sector_delta"] = round(
                float(item.get("open_daban_score") or 0.0) - leader_score,
                2,
            )
    return eligible


def rank_confirmations(
    shortlist: Sequence[Mapping[str, Any]],
    confirmations: Sequence[Mapping[str, Any]],
    limit: int = DEFAULT_OPEN_LIMIT,
    temperature: Mapping[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    eligible = score_confirmations(shortlist, confirmations)

    temperature_active = bool(
        temperature
        and temperature.get("tier") != "neutral"
        and temperature.get("context_fresh", True)
    )
    allow_new_daban = bool(
        not temperature_active or temperature.get("allow_new_daban", True)
    )
    daban_quota = (limit + 1) // 2
    if temperature_active:
        top_n_limit = temperature.get("top_n_limit")
        if not allow_new_daban:
            daban_quota = 0
        elif isinstance(top_n_limit, int):
            daban_quota = min(daban_quota, max(0, top_n_limit))
    trend_quota = max(0, limit - daban_quota)

    def _lane_member(item: Mapping[str, Any], lane: str) -> bool:
        if lane == "daban" and not allow_new_daban:
            return False
        if lane == "daban" and "hot_money_qualified" in item:
            if not item.get("hot_money_qualified"):
                return False
        selected_by = item.get("auction_selected_by") or item.get("selected_by")
        if isinstance(selected_by, Mapping):
            return bool(selected_by.get(lane))
        if lane == "daban":
            return candidate_pipeline.is_main_board_10cm(
                item.get("code"),
                str(item.get("name") or ""),
            )
        return True

    selected: Dict[str, Dict[str, Any]] = {}

    def _delivery_quality(item: Mapping[str, Any], lane: str) -> Dict[str, Any]:
        return assess_delivery_quality(item, lane=lane, stage="09:35")

    def _add_lane(lane: str, quota: int) -> None:
        if quota <= 0:
            return
        score_key = f"open_{lane}_score"
        ordered = sorted(
            (item for item in eligible if _lane_member(item, lane)),
            key=lambda row: (-float(row.get(score_key) or 0.0), str(row.get("code"))),
        )
        added = 0
        for item in ordered:
            code = candidate_pipeline.naked_code(item.get("code"))
            if code in selected:
                selected[code]["open_selected_by"][lane] = True
                continue
            delivery_quality = _delivery_quality(item, lane)
            if delivery_quality["status"] != "deliverable_watch":
                continue
            chosen = dict(item)
            chosen["delivery_quality"] = delivery_quality
            chosen["open_selected_by"] = {
                "daban": lane == "daban",
                "trend": lane == "trend",
                "balanced_fill": False,
            }
            selected[code] = chosen
            added += 1
            if added >= quota:
                break

    _add_lane("daban", daban_quota)
    _add_lane("trend", trend_quota)
    if len(selected) < limit:
        for item in sorted(
            eligible,
            key=lambda row: (-float(row.get("open_score") or 0.0), str(row.get("code"))),
        ):
            code = candidate_pipeline.naked_code(item.get("code"))
            if code in selected:
                continue
            if (
                "hot_money_qualified" in item
                and not item.get("hot_money_qualified")
                and not _lane_member(item, "trend")
            ):
                continue
            if temperature_active and not _lane_member(item, "trend"):
                continue
            lane = "trend" if _lane_member(item, "trend") else "daban"
            delivery_quality = _delivery_quality(item, lane)
            if delivery_quality["status"] != "deliverable_watch":
                continue
            chosen = dict(item)
            chosen["delivery_quality"] = delivery_quality
            chosen["open_selected_by"] = {
                "daban": False,
                "trend": temperature_active,
                "balanced_fill": not temperature_active,
            }
            selected[code] = chosen
            if len(selected) >= limit:
                break

    ranked = sorted(
        selected.values(),
        key=lambda item: (-item["open_score"], str(item.get("code"))),
    )[:limit]
    for index, item in enumerate(ranked, 1):
        item["open_rank"] = index
    return ranked


def _rejection_reasons(
    confirmations: Sequence[Mapping[str, Any]],
    selected_codes: set[str],
    limit: int,
) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for item in confirmations:
        code = candidate_pipeline.naked_code(item.get("code"))
        if code in selected_codes:
            continue
        reasons = list(item.get("reasons") or [])
        if item.get("action") in {"watch", "trend_watch"}:
            reasons = [f"开盘综合排名未进入前{limit}"]
        result[code] = reasons or ["开盘确认不足"]
    return result


def _fetch_snapshots(codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    unique_codes = list(dict.fromkeys(code for code in codes if code))
    quotes: Dict[str, Dict[str, Any]] = {}
    for index in range(0, len(unique_codes), QUOTE_BATCH_SIZE):
        quotes.update(
            fetch_tencent_snapshot(
                unique_codes[index:index + QUOTE_BATCH_SIZE]
            )
        )
    return quotes


def build_confirmation(
    codes: List[str],
    asof: str,
    limit: int = DEFAULT_OPEN_LIMIT,
) -> Dict[str, Any]:
    shortlist_result = load_shortlist(asof)
    factors = list(shortlist_result.get("shortlist", []))
    if codes:
        wanted = {candidate_pipeline.naked_code(code) for code in codes}
        factors = [
            factor for factor in factors
            if candidate_pipeline.naked_code(factor.get("code")) in wanted
        ]
    quote_codes = [
        factor["code"]
        for factor in factors
        if factor.get("code") and not factor.get("error")
    ]
    signal_ctx = read_signal_context(max_age_hours=4 * 24) or {}
    temperature = temperature_from_context(
        signal_ctx,
        event_asof=asof,
        max_age_days=4,
    )
    if temperature.get("context_fresh"):
        quote_codes.extend(
            candidate_pipeline.market_code(code)
            for code in (signal_ctx.get("lianban_ladder") or {})
        )
    raw_quotes = _fetch_snapshots(quote_codes) if quote_codes else {}
    input_snapshot = materialize_input_snapshot(
        "open-confirmation-input",
        {
            "schema": "open_confirmation_inputs_v1",
            "quotes": raw_quotes,
            "signal_context": signal_ctx,
        },
        trading_date=asof,
        batch_id=os.environ.get("A_STOCK_BATCH_ID") or f"a-share-{asof.replace('-', '')}",
        producer="open-confirmation",
        source_versions={
            "tencent": "tencent-adapter-v2",
            "akshare": "akshare-adapter-v1",
            **dict(
                (signal_ctx.get("social_attention") or {}).get(
                    "source_versions"
                ) or {}
            ),
        },
    )
    quotes = dict(input_snapshot["payload"]["quotes"])
    signal_ctx = dict(input_snapshot["payload"].get("signal_context") or {})
    temperature = temperature_from_context(
        signal_ctx,
        morning_quotes=quotes,
        event_asof=asof,
        max_age_days=4,
    )

    confirmations = []
    for factor in factors:
        quote = quotes.get(factor.get("code"), {})
        confirmations.append(evaluate_open_confirmation(factor, quote, asof=asof))

    signals = rank_confirmations(
        factors,
        confirmations,
        limit=limit,
        temperature=temperature,
    )
    scored_by_code = {
        candidate_pipeline.naked_code(item.get("code")): item
        for item in score_confirmations(factors, confirmations)
    }
    prior_by_code = {
        candidate_pipeline.naked_code(item.get("code")): dict(item)
        for item in factors
    }
    evaluated_confirmations = []
    for confirmation in confirmations:
        code = candidate_pipeline.naked_code(confirmation.get("code"))
        evaluated_confirmations.append(
            dict(scored_by_code.get(code) or {**prior_by_code.get(code, {}), **confirmation})
        )
    announcement_map = scan_many(
        candidate_pipeline.naked_code(item.get("code"))
        for item in signals
    )
    portfolio = read_json(data_file("stock-triage", "portfolio.json"), {})
    regime = market_regime(read_market_context())
    signals = [
        _apply_policy(_enrich_decision(
            item,
            announcement_map.get(candidate_pipeline.naked_code(item.get("code"))),
            asof,
        ), asof=asof, portfolio=portfolio, regime=regime)
        for item in signals
    ]

    monitor_expiry = add_trading_days(asof, 2)
    batch_id = os.environ.get("A_STOCK_BATCH_ID") or f"a-share-{asof.replace('-', '')}"
    stock_monitor_targets = []
    sector_monitor_targets: dict[str, dict[str, Any]] = {}
    for item in signals:
        code = candidate_pipeline.naked_code(item.get("code"))
        recommendation_id = f"open-{asof}-{code}"
        recommendation_action = _recommendation_action(item)
        links = signal_ledger.make_links(
            recommendation_id,
            monitor_id=f"stock:{code}",
            include_trade=item.get("decision") in signal_ledger.TRADE_ACTIONS,
        )
        item["ledger_links"] = {
            key: value for key, value in links.items() if value is not None
        }
        if item.get("decision") == "avoid":
            monitor_registry.deactivate_automatic(
                "stock",
                code,
                reason="open_confirmation_rejected",
            )
        else:
            stock_monitor_targets.append({
                "code": code,
                "name": str(item.get("name") or code),
                "metadata": {
                    "decision": item.get("decision"),
                    "open_rank": item.get("open_rank"),
                    "quality_status": (item.get("quality_report") or {}).get("status"),
                    **item["ledger_links"],
                },
            })
        sector = str(item.get("sector") or "").strip()
        if sector and item.get("decision") != "avoid":
            sector_monitor_targets[sector] = {
                "key": sector,
                "label": sector,
                "metadata": {
                    "correlation_id": links["correlation_id"],
                    "recommendation_id": links["recommendation_id"],
                    "signal_id": links["signal_id"],
                },
            }
        plan = item.get("execution_plan") or {}
        quality = item.get("quality_report") or {}
        recommendation_audit.record_recommendation(
            code=code,
            name=str(item.get("name") or code),
            action=recommendation_action,
            price_range=str(plan.get("entry_range") or "N/A"),
            rationale="；".join(item.get("reasons") or ["09:35开盘确认"]),
            risks=list(quality.get("risk_warnings") or []) + list(plan.get("invalidation") or []),
            entry_price=item.get("price"),
            target_price=plan.get("target_price"),
            stop_price=plan.get("stop_price"),
            horizon=plan.get("horizon"),
            grade="A" if float(item.get("open_score") or 0) >= 80 else "B",
            confidence="medium",
            strategy_id=item["strategy_id"],
            announcements=item.get("announcements"),
            source_id=recommendation_id,
            asof=asof,
            correlation_id=links["correlation_id"],
            signal_id=links["signal_id"],
            trade_id=links["trade_id"],
            monitor_id=links["monitor_id"],
            research_evidence=item.get("research_evidence"),
            portfolio_risk=item.get("portfolio_risk"),
            social_attention={
                "candidate_bonus": item.get("social_attention_bonus"),
                "auction_delta": item.get("auction_social_attention_delta"),
                "notes": list(item.get("social_attention_notes") or []),
                "record": dict(item.get("social_attention") or {}),
            },
            selection_context=item.get("selection_context"),
        )
    monitor_registry.reconcile_automatic(
        "stock",
        stock_monitor_targets,
        source="open_confirmation",
        source_group="open_confirmation",
        trading_date=asof,
        batch_id=batch_id,
        expires_at=monitor_expiry,
    )
    monitor_registry.reconcile_automatic(
        "sector",
        sector_monitor_targets.values(),
        source="open_confirmation",
        source_group="open_confirmation",
        trading_date=asof,
        batch_id=batch_id,
        expires_at=monitor_expiry,
    )
    result = {
        "schema": "open_confirmation_v3",
        "asof": asof,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "ready",
        "source_asof": shortlist_result.get("source_asof"),
        "market_temperature": temperature,
        "market_regime": regime,
        "input_snapshot": compact_ref(input_snapshot),
        "confirmations": confirmations,
        "evaluated_confirmations": evaluated_confirmations,
        "signals": signals,
        "signal_count": len(signals),
    }
    research_snapshot = record_open_confirmation(result)
    result["portfolio_research_snapshot"] = {
        "status": research_snapshot["status"],
        "path": research_snapshot.get("path"),
        "snapshot_sha256": (
            (research_snapshot.get("snapshot") or {}).get("snapshot_sha256")
        ),
        "reason": research_snapshot.get("reason"),
    }
    atomic_write_json(_confirmation_path(asof), result)
    atomic_write_json(_confirmation_latest_path(), result)

    selected_codes = {
        candidate_pipeline.naked_code(item["code"])
        for item in signals
    }
    source_asof = str(shortlist_result.get("source_asof") or "")
    if source_asof:
        candidate_lifecycle.transition(
            source_asof,
            "open_confirmed",
            selected_codes,
            rejection_reasons=_rejection_reasons(confirmations, selected_codes, limit),
            event_asof=asof,
            details_by_code={
                candidate_pipeline.naked_code(item["code"]): {
                    "open_rank": item["open_rank"],
                    "open_score": item["open_score"],
                    "action": item["action"],
                    "strategy_id": item.get("strategy_id"),
                    "open_sector_rank": item.get("open_sector_rank"),
                    "hot_money_qualified": item.get("hot_money_qualified"),
                }
                for item in signals
            },
        )
    return result


def format_report(result: Dict[str, Any]) -> str:
    if not result["confirmations"]:
        return "09:35 开盘确认：无竞价候选"
    lines = [f"## 09:35 开盘确认 | {result['asof']}"]
    for item in result["confirmations"]:
        lines.append(
            f"- {item['name']}({item['code']}): {item['action']} "
            f"现价={item.get('price')} 涨幅={item.get('change_pct')}% "
            f"竞价={item.get('auction_gap_pct')}% | {'；'.join(item['reasons'])}"
        )
    if result.get("signals"):
        lines.append("\n### 策略门禁后的研究/执行状态")
        for item in result["signals"]:
            plan = item.get("execution_plan") or {}
            quality = item.get("quality_report") or {}
            context = item.get("selection_context") or {}
            sector = context.get("sector") or {}
            leader = context.get("leader") or {}
            lines.append(
                f"- {item['name']}({item['code']}): {item.get('decision')} | "
                f"策略={item.get('strategy_id')} | "
                f"板块={sector.get('name') or '-'}#{sector.get('rank') or '-'} "
                f"龙头#{leader.get('rank') or '-'} | "
                f"买入区间={plan.get('entry_range')} 最高追价={plan.get('max_chase_price')} "
                f"止损={plan.get('stop_price')} 目标={plan.get('target_price')} "
                f"仓位={plan.get('position_pct')}% | 质检={quality.get('status')} | "
                f"A股T+1，最早卖出={plan.get('earliest_sell_date')}"
            )
    return "\n".join(lines)


def json_report(result: Mapping[str, Any]) -> Dict[str, Any]:
    report = {
        "schema": result.get("schema"),
        "status": result.get("status"),
        "asof": result.get("asof"),
        "generated_at": result.get("generated_at"),
        "source_asof": result.get("source_asof"),
        "market_temperature": result.get("market_temperature"),
        "signal_count": result.get("signal_count", 0),
        "portfolio_research_snapshot": result.get("portfolio_research_snapshot"),
        "signals": [
            {
                "code": item.get("code"),
                "name": item.get("name"),
                "sector": item.get("sector"),
                "open_rank": item.get("open_rank"),
                "open_sector_rank": item.get("open_sector_rank"),
                "open_score": item.get("open_score"),
                "strategy_id": item.get("strategy_id"),
                "decision": item.get("decision"),
                "quality_status": (item.get("quality_report") or {}).get("status"),
                "policy_reasons": list((item.get("policy_decision") or {}).get("reasons") or []),
                "selection_context": hot_money_selection.compact_selection_context(
                    item.get("selection_context")
                ),
                "execution_plan": {
                    key: (item.get("execution_plan") or {}).get(key)
                    for key in (
                        "decision", "entry_range", "max_chase_price",
                        "stop_price", "target_price", "position_pct",
                        "earliest_sell_date", "same_day_sell_allowed",
                    )
                },
            }
            for item in result.get("signals") or []
        ],
    }
    report["intelligence"] = stage_intelligence.open_digest(result)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="A股09:35开盘确认")
    parser.add_argument("--codes", help="逗号分隔，带市场前缀，如 sh600519,sz000001")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_OPEN_LIMIT,
        help="开盘确认最终保留数量（默认读取 candidate_selection 配置）",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else []
    try:
        result = build_confirmation(codes, args.asof, limit=args.limit)
    except DataSourceError as exc:
        result = {
            "schema": "open_confirmation_v1",
            "asof": args.asof,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "insufficient_data",
            "error": str(exc),
            "confirmations": [],
            "signals": [],
            "signal_count": 0,
        }

    if args.json:
        print(json.dumps(json_report(result), ensure_ascii=False))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
