"""Aggregate behavioral-finance context for A-share decision support."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from behavior_risk import assess_behavior_risk
from crowding_fragility import build_market_crowding_fragility


SCHEMA = "behavioral_finance_context_v1"


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _quotes(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    for key in ("quotes", "stocks", "items", "universe", "market_quotes"):
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    data = payload.get("data")
    if isinstance(data, Mapping):
        return _quotes(data)
    return []


def _extract_social_score(payload: Mapping[str, Any] | None) -> tuple[float | None, list[str]]:
    unavailable: list[str] = []
    if not isinstance(payload, Mapping):
        return None, ["social_attention_missing"]
    for key in ("sentiment_score", "attention_score", "score"):
        value = _num(payload.get(key))
        if value is not None:
            return max(0.0, min(100.0, value)), unavailable
    items = payload.get("items") or payload.get("signals") or payload.get("events")
    if isinstance(items, list) and items:
        return min(100.0, 45.0 + len(items) * 5.0), unavailable
    unavailable.append("social_attention_score_unavailable")
    return None, unavailable


def _hot_counts(payload: Mapping[str, Any] | None) -> dict[str, int | None]:
    if not isinstance(payload, Mapping):
        return {"limit_up_count": None, "limit_down_count": None}
    limit_up = payload.get("limit_up_count")
    limit_down = payload.get("limit_down_count")
    if limit_up is None:
        for key in ("limit_ups", "zt_pool", "leaders"):
            value = payload.get(key)
            if isinstance(value, list):
                limit_up = len(value)
                break
    return {
        "limit_up_count": int(limit_up) if isinstance(limit_up, int) else None,
        "limit_down_count": int(limit_down) if isinstance(limit_down, int) else None,
    }


def _phase(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score < 25:
        return "fear"
    if score < 45:
        return "caution"
    if score < 65:
        return "neutral_to_optimism"
    if score < 80:
        return "optimism_to_excitement"
    return "euphoria"


def _strategy_adjustments(score: float | None, crowding: Mapping[str, Any]) -> dict[str, Any]:
    notes: list[str] = []
    multiplier = 1.0
    exposure_band = "normal"
    fragility = _num(crowding.get("fragility_score"))
    crowding_score = _num(crowding.get("crowding_score"))
    if score is not None and score >= 80:
        exposure_band = "tighten"
        multiplier = 0.5
        notes.append("情绪过热：只允许收紧追价与仓位条件")
    elif score is not None and score <= 25:
        exposure_band = "research_only"
        notes.append("极端恐惧：只提高研究优先级，不绕过买入门禁")
    if (fragility is not None and fragility >= 0.55) or (crowding_score is not None and crowding_score >= 0.6):
        multiplier = min(multiplier, 0.7)
        notes.append("拥挤/脆弱性偏高：缩短高关注票验证窗口")
    return {
        "momentum_holding_days_multiplier": multiplier,
        "exposure_band": exposure_band,
        "notes": notes,
    }


def build_behavioral_finance_context(
    market_snapshot: Mapping[str, Any] | None,
    social_attention: Mapping[str, Any] | None,
    hot_money_context: Mapping[str, Any] | None,
    signal_state: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    *,
    asof: str,
    trading_date: str | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Build the unified context without external dependencies or fetches."""
    unavailable: list[str] = []
    quotes = _quotes(market_snapshot)
    if quotes:
        crowding = build_market_crowding_fragility(quotes, event_asof=asof)
        if crowding.get("status") != "ready":
            unavailable.append("crowding_fragility_insufficient_data")
    else:
        crowding = {
            "schema": "market_crowding_fragility_v1",
            "status": "insufficient_data",
            "observed": 0,
            "crowding_score": None,
            "fragility_score": None,
            "components": {},
            "signals": [],
        }
        unavailable.append("market_snapshot_quotes_missing")

    social_score, social_unavailable = _extract_social_score(social_attention)
    unavailable.extend(social_unavailable)
    hot_counts = _hot_counts(hot_money_context)
    if hot_counts["limit_up_count"] is None:
        unavailable.append("limit_up_count_unavailable")

    score_parts: list[float] = []
    if social_score is not None:
        score_parts.append(social_score)
    if hot_counts["limit_up_count"] is not None:
        score_parts.append(min(100.0, 40.0 + float(hot_counts["limit_up_count"]) * 2.0))
    crowd_score = _num(crowding.get("crowding_score"))
    if crowd_score is not None:
        score_parts.append(crowd_score * 100.0)
    sentiment_score = round(sum(score_parts) / len(score_parts), 2) if score_parts else None

    signals: Sequence[Mapping[str, Any]]
    if isinstance(signal_state, Mapping):
        raw = signal_state.get("signals") or signal_state.get("items") or []
        signals = raw if isinstance(raw, list) else []
    elif isinstance(signal_state, list):
        signals = signal_state
    else:
        signals = []
    agent_risk = assess_behavior_risk(signals, asof=(trading_date or asof[:10]))

    overreaction_market: list[dict[str, Any]] = []
    if sentiment_score is not None and sentiment_score >= 80:
        overreaction_market.append({"type": "euphoria", "score": sentiment_score})
    if crowding.get("signals"):
        overreaction_market.append({"type": "crowding_fragility", "signals": crowding.get("signals")})
    underreaction_stocks: list[dict[str, Any]] = []

    checklist = [
        "不要因连胜扩大单笔风险预算",
        "高关注票先确认承接再行动",
        "极端情绪只改变研究优先级，不绕过门禁",
    ]
    if agent_risk.get("flags"):
        checklist.extend(str(flag) for flag in agent_risk["flags"][:3])

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trading_date": trading_date or asof[:10],
        "batch_id": batch_id,
        "status": "ready" if score_parts or crowding.get("status") == "ready" else "degraded",
        "sentiment_score": sentiment_score,
        "sentiment_phase": _phase(sentiment_score),
        "overreaction": {"market": overreaction_market, "stocks": []},
        "underreaction": {"stocks": underreaction_stocks},
        "crowding_fragility": crowding,
        "agent_behavior_risk": agent_risk,
        "strategy_adjustments": _strategy_adjustments(sentiment_score, crowding),
        "debiasing_checklist": checklist,
        "summary": {
            "sentiment_phase": _phase(sentiment_score),
            "sentiment_score": sentiment_score,
            "risk_flags": len(overreaction_market) + len(agent_risk.get("flags") or []),
        },
        "has_signal": bool(overreaction_market or agent_risk.get("flags")),
        "missing_errors": [],
        "unavailable": sorted(set(unavailable + list(agent_risk.get("unavailable") or []))),
    }
