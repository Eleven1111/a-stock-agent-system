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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SCRIPT_DIR = os.path.dirname(__file__)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "..", "stock-triage", "scripts"))

from a_stock_http import DataSourceError  # noqa: E402
from market_adapters import fetch_tencent_snapshot  # noqa: E402
from announcement_risk import scan_many  # noqa: E402
from a_share_rules import add_trading_days, resolve_price_limit_rule  # noqa: E402
from execution_model import build_execution_scenarios, estimate_trade_cost  # noqa: E402
import candidate_fsm  # noqa: E402
import candidate_lifecycle  # noqa: E402
import candidate_pipeline  # noqa: E402
import hot_money_selection  # noqa: E402
import stage_intelligence  # noqa: E402
from weak_market_delivery import assess_delivery_quality  # noqa: E402
from market_temperature import block_new_risk, temperature_from_context  # noqa: E402
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
from market_snapshot import (  # noqa: E402
    PointInTimeViolation,
    compact_ref,
    materialize_input_snapshot,
    validate_point_in_time,
)
from market_context import market_regime, read_market_context  # noqa: E402
from portfolio_policy import (  # noqa: E402
    evaluate_candidate,
    evaluate_complete_admission,
    portfolio_value,
)
from portfolio_research_history import record_open_confirmation  # noqa: E402
import recommendation_audit  # noqa: E402
from research_evidence import build_research_evidence  # noqa: E402
import signal_ledger  # noqa: E402
import strategy_registry  # noqa: E402
import trading_discipline  # noqa: E402
from signal_context import read_signal_context  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402
from tradeability import assess_tradeability  # noqa: E402

QUOTE_BATCH_SIZE = 80
POSITIVE_ACTIONS = {"buy", "add", "conditional_buy"}


# The quote endpoint is deliberately kept small.  These helpers therefore
# accept richer, optional intraday evidence supplied by a caller (or by a
# replay fixture) without making the 09:35 path depend on a second provider.
# Keeping the derivation here also makes old shortlist/quote artifacts valid.
_MISSING = object()


def _metric_value(*objects: Mapping[str, Any], names: Sequence[str]) -> Any:
    """Return the first present metric, including common nested containers."""
    containers: list[Mapping[str, Any]] = []
    for obj in objects:
        if not isinstance(obj, Mapping):
            continue
        containers.append(obj)
        for key in ("intraday", "intraday_metrics", "open_metrics", "market_metrics"):
            value = obj.get(key)
            if isinstance(value, Mapping):
                containers.append(value)
        for key in ("selection_context", "market_timing", "breadth", "sector_evidence"):
            value = obj.get(key)
            if isinstance(value, Mapping):
                containers.append(value)
                for nested in ("market_timing", "breadth", "sector", "metrics"):
                    child = value.get(nested)
                    if isinstance(child, Mapping):
                        containers.append(child)
    for container in containers:
        for name in names:
            if (name in container and container[name] is not None
                    and not isinstance(container[name], Mapping)):
                return container[name]
    return _MISSING


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _intraday_rows(*objects: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for obj in objects:
        if not isinstance(obj, Mapping):
            continue
        for key in ("intraday_bars", "minute_bars", "bars", "minutes", "分时"):
            rows = obj.get(key)
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                return [row for row in rows if isinstance(row, Mapping)]
    return []


def _intraday_series(
    rows: Sequence[Mapping[str, Any]],
) -> Tuple[list[float], list[float], list[float]]:
    """Split minute rows into (prices, above-vwap flags, volumes)."""
    prices: list[float] = []
    vwaps: list[float] = []
    volumes: list[float] = []
    for row in rows:
        price = _as_float(row.get("price", row.get("close", row.get("最新价"))))
        vwap = _as_float(row.get("vwap", row.get("VWAP", row.get("均价"))))
        if price is not None and price > 0:
            prices.append(price)
            if vwap is not None and vwap > 0:
                vwaps.append(1.0 if price > vwap else 0.0)
        volume = _as_float(row.get("volume", row.get("cum_volume", row.get("成交量"))))
        if volume is not None:
            volumes.append(volume)
    return prices, vwaps, volumes


def _drawdown_pct(prices: Sequence[float], period: int) -> Optional[float]:
    sample = list(prices[:period])
    if not sample or sample[-1] <= 0:
        return None
    peak = max(sample)
    return max(0.0, (peak - sample[-1]) / peak * 100.0) if peak > 0 else None


def _rounded(value: Optional[float]) -> Optional[float]:
    return round(value, 4) if value is not None else None


def _direct_volume_ratio(direct: Any) -> Tuple[Optional[float], Optional[float]]:
    """Return (prior_volume, relative_volume) from provider-supplied fields."""
    open_volume = _as_float(direct(("open_volume", "opening_volume", "开盘量")))
    if open_volume is None:
        # Tencent's 09:35 ``volume`` is the session opening accumulation.
        open_volume = _as_float(direct(("volume", "成交量")))
    prior_volume = _as_float(direct((
        "previous_volume", "prev_volume", "yesterday_volume", "昨日量", "前日成交量",
    )))
    relative_volume = _as_float(direct((
        "open_relative_volume", "opening_relative_volume", "open_volume_ratio",
        "opening_volume_ratio", "relative_open_volume", "开盘相对量",
    )))
    if relative_volume is None and open_volume is not None and prior_volume and prior_volume > 0:
        relative_volume = open_volume / prior_volume
    return prior_volume, relative_volume


def _direct_vwap_ratio(direct: Any) -> Optional[float]:
    ratio = _as_float(direct((
        "vwap_above_time_ratio", "vwap_above_ratio", "above_vwap_ratio",
        "vwap_above_pct", "VWAP上方时间占比",
    )))
    if ratio is not None:
        return ratio
    above_minutes = _as_float(direct(("vwap_above_minutes", "above_vwap_minutes", "VWAP上方分钟")))
    observed_minutes = _as_float(direct(("vwap_observation_minutes", "observed_minutes", "分时观测分钟")))
    if above_minutes is not None and observed_minutes and observed_minutes > 0:
        return above_minutes / observed_minutes
    return None


def _direct_drawdowns(direct: Any) -> Tuple[Optional[float], Optional[float]]:
    dd15 = _as_float(direct((
        "open_15m_drawdown_pct", "opening_15m_drawdown_pct", "drawdown_15m_pct",
        "open15_drawdown_pct", "开盘15分钟回撤",
    )))
    dd30 = _as_float(direct((
        "open_30m_drawdown_pct", "opening_30m_drawdown_pct", "drawdown_30m_pct",
        "open30_drawdown_pct", "开盘30分钟回撤",
    )))
    return dd15, dd30


def derive_open_metrics(factor: Mapping[str, Any], quote: Mapping[str, Any]) -> Dict[str, Any]:
    """Derive optional open-quality evidence from a factor/quote pair.

    Values are ``None`` when unavailable: callers must not turn a data gap into
    a positive signal.  Intraday rows may contain either price/close and vwap,
    and cumulative or per-bar volume.  Direct fields win so replay artifacts
    can preserve the provider's calculation.
    """
    def _direct(names): return _metric_value(quote, factor, names=names)
    prior_volume, relative_volume = _direct_volume_ratio(_direct)
    rows = _intraday_rows(quote, factor)
    vwap_ratio = _direct_vwap_ratio(_direct)
    dd15, dd30 = _direct_drawdowns(_direct)
    if rows:
        prices, vwaps, volumes = _intraday_series(rows)
        if vwap_ratio is None and vwaps:
            vwap_ratio = sum(vwaps) / len(vwaps)
        if dd15 is None:
            dd15 = _drawdown_pct(prices, 15)
        if dd30 is None:
            dd30 = _drawdown_pct(prices, 30)
        if relative_volume is None and volumes and prior_volume and prior_volume > 0:
            # A cumulative first-window volume is the least surprising meaning
            # of "open volume" for minute fixtures.
            relative_volume = volumes[min(len(volumes), 15) - 1] / prior_volume

    sector_limitups = _as_float(_direct((
        "sector_limitup_count", "sector_limitups", "sector_limitup_diffusion",
        "sector_limitup_breadth", "板块涨停数", "板块涨停扩散",
    )))
    sector_breakouts = _as_float(_direct((
        "sector_breakout_count", "sector_breakouts", "sector_breakout_diffusion",
        "sector_probe_count", "sector_冲板数", "板块冲板数", "冲板扩散",
    )))
    seal_persistence = _direct((
        "seal_persistence", "seal_duration_minutes", "limitup_persistence",
        "封板持续性", "封板持续分钟",
    ))
    reseal_persistence = _direct((
        "reseal_persistence", "reseal_duration_minutes", "reclose_persistence",
        "reclose_continuity", "回封持续性", "回封持续分钟",
    ))
    limitups, breakouts = _rounded(sector_limitups), _rounded(sector_breakouts)
    seal = seal_persistence if seal_persistence is not _MISSING else None
    reseal = reseal_persistence if reseal_persistence is not _MISSING else None
    return {
        "open_relative_volume": _rounded(relative_volume),
        "opening_relative_volume": _rounded(relative_volume),
        "vwap_above_time_ratio": _rounded(vwap_ratio),
        "vwap_above_ratio": _rounded(vwap_ratio),
        "open_15m_drawdown_pct": _rounded(dd15),
        "drawdown_15m_pct": _rounded(dd15),
        "open_30m_drawdown_pct": _rounded(dd30),
        "drawdown_30m_pct": _rounded(dd30),
        "sector_limitup_diffusion": limitups,
        "sector_limitup_count": limitups,
        "sector_breakout_diffusion": breakouts,
        "sector_breakout_count": breakouts,
        "sector_diffusion": {"limitup_count": limitups, "breakout_count": breakouts},
        "seal_persistence": seal,
        "reseal_persistence": reseal,
        "seal_continuity": seal,
        "reseal_continuity": reseal,
        "reclose_continuity": reseal,
    }


def _quality_component(value: Any, *, kind: str) -> Optional[float]:
    """Map optional evidence to [0, 1]; None remains an explicit data gap."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    number = _as_float(value)
    if number is None:
        return None
    if kind == "volume":
        return max(0.0, min(1.0, (number - 0.5) / 1.5))
    if kind == "drawdown":
        return max(0.0, min(1.0, 1.0 - number / 10.0))
    if kind == "count":
        return max(0.0, min(1.0, number / 3.0))
    if kind == "minutes":
        return max(0.0, min(1.0, number / 15.0))
    return max(0.0, min(1.0, number))


def _lane_score(auction: float, action_quality: float, open_quality: float,
                metrics: Mapping[str, Any], lane: str) -> float:
    """Independent lane models; unavailable terms fall back to open quality."""
    volume = _quality_component(metrics.get("open_relative_volume"), kind="volume")
    vwap = _quality_component(metrics.get("vwap_above_time_ratio"), kind="ratio")
    drawdown15 = _quality_component(metrics.get("open_15m_drawdown_pct"), kind="drawdown")
    drawdown30 = _quality_component(metrics.get("open_30m_drawdown_pct"), kind="drawdown")
    diffusion = _quality_component(metrics.get("sector_limitup_diffusion"), kind="count")
    breakout = _quality_component(metrics.get("sector_breakout_diffusion"), kind="count")
    seal = _quality_component(metrics.get("seal_persistence"), kind="minutes")
    reseal = _quality_component(metrics.get("reseal_persistence"), kind="minutes")
    fallback = max(0.0, min(1.0, open_quality))
    if lane == "daban":
        components = (volume, diffusion, breakout, seal, reseal)
        values = [fallback if item is None else item for item in components]
        score = (0.45 * auction + 15.0 * action_quality + 10.0 * fallback
                 + 8.0 * values[0] + 8.0 * values[1] + 5.0 * values[2]
                 + 10.0 * values[3] + 9.0 * values[4])
    else:
        values = [fallback if item is None else item for item in (volume, vwap, drawdown15, drawdown30, breakout)]
        score = (0.35 * auction + 12.0 * action_quality + 12.0 * fallback
                 + 7.0 * values[0] + 14.0 * values[1] + 9.0 * values[2]
                 + 7.0 * values[3] + 7.0 * values[4])
    return round(max(0.0, min(100.0, score)), 2)


def _naked_code(code: str) -> str:
    return code[2:] if code.startswith(("sh", "sz")) else code


def _advance_fsm_to_candidate(asof: str, signals: Sequence[Mapping[str, Any]]) -> None:
    """Route 09:35 open-confirmed signals through the FSM: watching -> candidate
    for anything with a buy-side decision. Idempotent: codes already at
    candidate/confirmed are left alone so re-runs don't spam rejected events.
    Best-effort: never blocks the open-confirmation output, which is the
    authoritative confirmation surface."""
    config = candidate_fsm.load_fsm_config()
    for item in signals:
        if _recommendation_action(item) not in POSITIVE_ACTIONS:
            continue
        code = _naked_code(str(item.get("code") or ""))
        if not code:
            continue
        try:
            state = candidate_fsm.current_state(code)
            if state is not None and state.get("to_state") != "watching":
                continue
            candidate_fsm.transition(
                code, "candidate", "score_above_threshold", asof=asof, config=config,
            )
        except Exception:  # noqa: BLE001
            continue


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


def _open_execution_controls(
    item: Mapping[str, Any],
    quote: Mapping[str, Any],
    tradeability: Mapping[str, Any],
    asof: str,
) -> Dict[str, Any]:
    point_in_time = item.get("point_in_time") or quote.get("point_in_time")
    if not isinstance(point_in_time, Mapping):
        return {"status": "blocked", "reason": "point_in_time_missing"}
    try:
        validated_pit = validate_point_in_time(
            event_asof=str(point_in_time.get("event_asof") or ""),
            evidence_time=str(point_in_time.get("evidence_time") or ""),
            captured_at=str(point_in_time.get("captured_at") or ""),
            decision_mode=str(point_in_time.get("decision_mode") or ""),
            stage_policy=point_in_time.get("stage_policy") or {},
        )
    except PointInTimeViolation:
        return {"status": "blocked", "reason": "point_in_time_invalid"}
    if validated_pit.get("event_asof") != asof:
        return {"status": "blocked", "reason": "point_in_time_mismatch"}
    if validated_pit.get("decision_mode") != item.get("decision_mode"):
        return {"status": "blocked", "reason": "point_in_time_mismatch"}
    rule = resolve_price_limit_rule(
        code=_naked_code(str(item.get("code") or "")),
        asof=asof,
        listing_date=item.get("listing_date"),
        listing_stage=item.get("listing_stage"),
        is_st=item.get("is_st"),
        direction="buy",
    )
    if rule["status"] != "known":
        return {"status": "blocked", "reason": rule["reason"], "rule": rule}
    if quote.get("directional_eligible") is not True:
        return {"status": "blocked", "reason": "transport_lower_trust", "rule": rule}
    price = quote.get("price")
    if not isinstance(price, (int, float)) or price <= 0:
        return {"status": "blocked", "reason": "execution_price_unknown", "rule": rule}
    quantity = int(item.get("planned_quantity") or 100)
    scenarios = build_execution_scenarios(
        side="buy",
        quantity=quantity,
        signal_price=float(price),
        limit_queue=tradeability.get("status") == "limit_up",
        executable_price=(
            float(price) if tradeability.get("tradeable") is not False else None
        ),
        available_volume=quote.get("volume"),
        adv_value=item.get("adv_value"),
        event_asof=asof,
    )
    return {
        "status": "estimate_only",
        "reason": "broker_reconciliation_required",
        "rule": rule,
        "point_in_time": dict(validated_pit),
        "scenario_quantity": quantity,
        "scenario_quantity_basis": (
            "planned_quantity" if item.get("planned_quantity") else "minimum_lot"
        ),
        "scenarios": scenarios,
        "entry_cost_estimate": estimate_trade_cost(
            "buy", float(price) * quantity, asof=asof
        ),
        "authoritative_source": "broker_statement",
    }


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


def _carry_prior_chanlun(evidence: Dict[str, Any], result: Mapping[str, Any]) -> None:
    """Keep the 09:25 chanlun read; the 09:35 rebuild must not silently drop it."""
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


def _portfolio_risk(
    result: Mapping[str, Any],
    portfolio: Mapping[str, Any] | None,
    *,
    asof: str | None,
) -> Dict[str, Any]:
    position_pct = float((result.get("execution_plan") or {}).get("position_pct") or 0)
    if result.get("strict_execution") is True:
        return evaluate_complete_admission(
            portfolio or {},
            result,
            position_pct,
            factor_evidence=result.get("portfolio_risk_evidence"),
            decision_asof=str(asof or date.today().isoformat()),
        )
    return evaluate_candidate(portfolio or {}, result, position_pct)


def _record_strategy_migration(result: Dict[str, Any], strategy_id: str) -> None:
    """Leave a trace when 09:35 re-identifies the candidate into another strategy."""
    prior_identity = result.get("strategy_identity") or result.get("open_strategy_identity")
    if not prior_identity or prior_identity == strategy_id:
        return
    result["migration_from"] = prior_identity
    result["strategy_state_event"] = {
        "event": "strategy_migration",
        "from": prior_identity,
        "to": strategy_id,
        "window": "09:35",
    }


def _apply_policy(
    item: Mapping[str, Any],
    asof: str | None = None,
    portfolio: Mapping[str, Any] | None = None,
    regime: Mapping[str, Any] | None = None,
    discipline_state: Mapping[str, Any] | None = None,
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
    _carry_prior_chanlun(evidence, result)
    portfolio_risk = _portfolio_risk(result, portfolio, asof=asof)
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
        discipline_state=discipline_state,
    )
    result["strategy_id"] = strategy_id
    _record_strategy_migration(result, strategy_id)
    result["strategy_identity"] = strategy_id
    result["exit_protocol"] = (
        "daban:t1_event_exit_v1"
        if lane == "daban"
        else "trend:state_atr_exit_v1"
    )
    result["selection_context"] = hot_money_selection.advance_selection_context(
        result,
        window="09:35",
    )
    result["research_evidence"] = evidence
    result["portfolio_risk"] = portfolio_risk
    result["market_regime"] = dict(regime or {})
    result["discipline_state"] = dict(discipline_state or {})
    result["policy_decision"] = policy
    if policy["decision"] in {"avoid", "watch"}:
        plan = dict(result.get("execution_plan") or {})
        plan["decision"] = policy["decision"]
        plan["position_pct"] = 0.0
        result["execution_plan"] = plan
        result["decision"] = policy["decision"]
    return result


INDICATIVE_PRICE_MAX_DEVIATION_PCT = 2.0


def _indicative_price_reliability(
    factor: Mapping[str, Any],
    quote: Mapping[str, Any],
) -> Tuple[Optional[float], Optional[bool]]:
    """09:25 指示价 vs 实际开盘价的偏差（%）与可信标记。

    免费源的竞价指示价可能被撤单和镜像盘口污染；开盘价一出来就能事后校验一次。
    数据不全时返回 (None, None) —— 不知道就不表态（issue #140 P2）。
    """
    indicative = factor.get("indicative_price")
    open_price = quote.get("open")
    if indicative in (None, 0) or open_price in (None, 0):
        return None, None
    deviation = round((float(open_price) - float(indicative)) / float(indicative) * 100, 2)
    return deviation, abs(deviation) <= INDICATIVE_PRICE_MAX_DEVIATION_PCT


def _open_action(
    factor: Mapping[str, Any],
    tradeability: Mapping[str, Any],
    *,
    change_pct: Any,
    indicative_reliable: Optional[bool],
    indicative_deviation_pct: Optional[float],
) -> Tuple[str, List[str]]:
    """Classify the 09:35 open into an action plus its human-readable reasons."""
    action = "skip"
    reasons: List[str] = []
    if indicative_reliable is False:
        reasons.append(
            f"09:25竞价指示价与实际开盘价偏差{indicative_deviation_pct:+.2f}%，"
            f"竞价因子标记不可信"
        )
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
    return action, reasons


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
    indicative_deviation_pct, indicative_reliable = _indicative_price_reliability(factor, quote)
    open_metrics = derive_open_metrics(factor, quote)

    action, reasons = _open_action(
        factor,
        tradeability,
        change_pct=change_pct,
        indicative_reliable=indicative_reliable,
        indicative_deviation_pct=indicative_deviation_pct,
    )

    result = {
        "code": code,
        "name": name,
        "price": price,
        "prev_close": quote.get("prev_close"),
        "change_pct": change_pct,
        "auction_gap_pct": factor.get("auction_gap_pct"),
        "auction_indicative_deviation_pct": indicative_deviation_pct,
        "auction_indicative_reliable": indicative_reliable,
        "board_status": factor.get("board_status"),
        "action": action,
        "tradeability": tradeability,
        "volume": quote.get("volume"),
        "adv_value": factor.get("adv_value"),
        "strict_execution": True,
        "decision_mode": factor.get("decision_mode"),
        "point_in_time": factor.get("point_in_time") or quote.get("point_in_time"),
        "listing_date": factor.get("listing_date"),
        "listing_stage": factor.get("listing_stage"),
        "is_st": factor.get("is_st"),
        "directional_eligible": quote.get("directional_eligible"),
        "corporate_action_status": factor.get("corporate_action_status"),
        "portfolio_risk_evidence": factor.get("portfolio_risk_evidence"),
        # Optional execution-quality evidence.  ``None`` means unavailable,
        # and is intentionally retained in the artifact for backtesting.
        **open_metrics,
        "reasons": reasons,
    }
    controls = _open_execution_controls(result, quote, tradeability, asof or date.today().isoformat())
    enriched = _enrich_decision(
        result,
        factor.get("announcements") if "announcements" in factor else None,
        asof or date.today().isoformat(),
    )
    enriched["execution_controls"] = controls
    if controls["status"] == "blocked":
        plan = dict(enriched.get("execution_plan") or {})
        plan.update({"decision": "watch", "position_pct": 0.0})
        enriched["execution_plan"] = plan
        enriched["decision"] = "watch"
        enriched.setdefault("reasons", []).append(str(controls["reason"]))
    return enriched


def _shortlist_path(asof: str) -> str:
    return data_file("daban-stock-picker", f"auction_shortlist_{asof}.json")


def _confirmation_path(asof: str) -> str:
    return data_file("daban-stock-picker", f"open_confirmation_{asof}.json")


def _confirmation_latest_path() -> str:
    return data_file("daban-stock-picker", "open_confirmation_latest.json")


def _evidence_sources(
    *,
    input_snapshot: Mapping[str, Any],
    shortlist_result: Mapping[str, Any],
    asof: str,
) -> List[Dict[str, Any]]:
    sources: List[Dict[str, Any]] = [{
        "source": "open-confirmation",
        "artifact": compact_ref(input_snapshot),
        "weight_hint": "primary",
    }, {
        "source": "auction-finalize",
        "artifact": {
            "path": _shortlist_path(asof),
            "schema": shortlist_result.get("schema"),
            "asof": shortlist_result.get("asof"),
            "source_asof": shortlist_result.get("source_asof"),
        },
        "weight_hint": "supporting",
    }]
    social_ref = shortlist_result.get("social_attention_snapshot")
    if social_ref:
        sources.append({
            "source": "social-attention",
            "artifact": social_ref,
            "weight_hint": "context",
        })
    return sources


def load_shortlist(asof: str) -> Dict[str, Any]:
    shortlist = read_json(_shortlist_path(asof), {})
    if not isinstance(shortlist, dict) or shortlist.get("asof") != asof:
        raise DataSourceError("auction_shortlist", f"{asof} 竞价短名单缺失")
    return shortlist


def shortlist_degradation(shortlist_result: Mapping[str, Any]) -> Optional[str]:
    """竞价短名单的降级原因；未降级返回 None。

    空短名单有两种截然相反的含义：竞价跑完了但无人入选（今天没有机会），
    或竞价根本没采到数据（今天没有观测，issue #112 / #113）。后者由
    auction-finalize 标记 status=degraded；不透传就会被误读成前者。
    """
    if str(shortlist_result.get("status")) != "degraded":
        return None
    reasons = [str(item) for item in shortlist_result.get("degraded_reasons") or []]
    detail = "；".join(reasons) or "原因未记录"
    collection = shortlist_result.get("collection_status") or "unknown"
    return f"竞价短名单降级（collection_status={collection}）：{detail}"


def _score_confirmation(
    item: Mapping[str, Any],
    prior_entry: Mapping[str, Any],
    quality: float,
) -> Dict[str, Any]:
    """Score one confirmed candidate on both lanes and resolve its strategy state."""
    merged = {**prior_entry, **item}
    change_pct = float(merged.get("change_pct") or 0.0)
    open_quality = max(0.0, 1.0 - abs(change_pct - 5.5) / 6.0)
    auction_score = float(merged.get("auction_score") or 0.0)
    daban_auction = (
        float(merged["auction_daban_score"])
        if "auction_daban_score" in merged
        else auction_score
    )
    trend_auction_raw = (
        float(merged["auction_trend_score_raw"])
        if "auction_trend_score_raw" in merged
        else float(merged["auction_trend_score"])
        if "auction_trend_score" in merged
        else auction_score
    )
    metrics = derive_open_metrics(merged, merged)
    merged.update(metrics)
    # Daban and trend deliberately have separate models.  In particular,
    # seal/reseal continuity is a Daban feature while VWAP persistence and
    # drawdown are trend features; neither lane inherits the other's score.
    merged["open_daban_score"] = _lane_score(
        daban_auction, quality, open_quality, metrics, "daban"
    )
    merged["open_trend_score"] = _lane_score(
        trend_auction_raw, quality, open_quality, metrics, "trend"
    )
    merged["open_action_models"] = {
        "daban": {
            "score": merged["open_daban_score"],
            "auction_score": daban_auction,
            "model": "seal_reseal_diffusion_v1",
        },
        "trend": {
            "score": merged["open_trend_score"],
            "auction_score": trend_auction_raw,
            "model": "vwap_drawdown_participation_v1",
        },
    }
    merged["open_trend_score_raw"] = merged["open_trend_score"]
    live_trend_weight = candidate_pipeline.resolve_trend_live_weight(
        merged.get("trend_live_weight")
    )
    merged["trend_live_weight"] = live_trend_weight
    merged["open_trend_score"] = round(merged["open_trend_score"] * live_trend_weight, 2)
    merged["open_action_models"]["trend"]["raw_score"] = merged["open_trend_score_raw"]
    merged["open_action_models"]["trend"]["score"] = merged["open_trend_score"]
    state = candidate_pipeline.strategy_state(
        merged,
        merged["open_daban_score"],
        merged["open_trend_score"],
        live_trend_weight,
    )
    merged.update({
        "open_strategy_identity": state["strategy_identity"],
        "open_primary_strategy_id": state["primary_strategy_id"],
        "open_exit_protocol": state["exit_protocol"],
        "open_primary_net_expectancy": state["primary_net_expectancy"],
        "open_primary_confidence": state["primary_confidence"],
        "open_migration_from": state["migration_from"],
        "open_strategy_state_event": state["strategy_state_event"],
        "open_strategy_state": state["strategy_state"],
        "open_score": state["strategy_live_score"],
    })
    return merged


def _assign_sector_ranks(eligible: Sequence[Dict[str, Any]]) -> None:
    """Rank each sector's members against their own leader, in place."""
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


def score_confirmations(
    shortlist: Sequence[Mapping[str, Any]],
    confirmations: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    prior = {
        candidate_pipeline.naked_code(item.get("code")): dict(item)
        for item in shortlist
    }
    action_quality = {"trend_watch": 1.0, "watch": 0.8}
    eligible = [
        _score_confirmation(
            dict(raw),
            prior.get(candidate_pipeline.naked_code(raw.get("code")), {}),
            action_quality[raw["action"]],
        )
        for raw in confirmations
        if raw.get("action") in action_quality
    ]
    _assign_sector_ranks(eligible)
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
    degradation = shortlist_degradation(shortlist_result)
    if degradation:
        temperature = block_new_risk(temperature, degradation)
    evidence_sources = _evidence_sources(
        input_snapshot=input_snapshot,
        shortlist_result=shortlist_result,
        asof=asof,
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
    discipline_state = trading_discipline.assess_discipline_state(
        signal_ledger.read_events(),
        total_assets=portfolio_value(portfolio),
        asof=asof,
    )
    signals = [
        _apply_policy(_enrich_decision(
            item,
            announcement_map.get(candidate_pipeline.naked_code(item.get("code"))),
            asof,
        ), asof=asof, portfolio=portfolio, regime=regime, discipline_state=discipline_state)
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
            discipline_state=item.get("discipline_state"),
            evidence_sources=evidence_sources,
            sector=item.get("sector"),
            execution_context={
                "strict_execution": item.get("strict_execution") is True,
                "decision_mode": item.get("decision_mode") or "live",
                "point_in_time": (item.get("execution_controls") or {}).get(
                    "point_in_time"
                ),
                "listing_date": item.get("listing_date"),
                "listing_stage": item.get("listing_stage"),
                "is_st": item.get("is_st"),
                "direction": "buy",
                "limit_queue": (item.get("tradeability") or {}).get("status") == "limit_up",
                "executable_price": item.get("price"),
                "available_volume": item.get("volume"),
                "adv_value": item.get("adv_value"),
                "directional_eligible": item.get("directional_eligible"),
                "corporate_action_status": item.get("corporate_action_status") or "unknown",
            },
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
        "status": "degraded" if degradation else "ready",
        "degraded_reasons": [degradation] if degradation else [],
        "source_asof": shortlist_result.get("source_asof"),
        "market_temperature": temperature,
        "market_regime": regime,
        "discipline_state": discipline_state,
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
    _advance_fsm_to_candidate(asof, signals)
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
        "discipline_state": result.get("discipline_state"),
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
