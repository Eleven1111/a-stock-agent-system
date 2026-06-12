"""Single deterministic policy gate for recommendations from any runtime."""

from __future__ import annotations

from typing import Any, Mapping, Optional


POSITIVE_ACTIONS = {"buy", "add", "conditional_buy"}
EXIT_ACTIONS = {"sell", "reduce"}


def evaluate_decision(
    *,
    requested_action: str,
    quality_report: Mapping[str, Any],
    strategy_record: Optional[Mapping[str, Any]] = None,
    t1_block: Optional[Mapping[str, Any]] = None,
    market_regime: Optional[Mapping[str, Any]] = None,
    portfolio_risk: Optional[Mapping[str, Any]] = None,
    research_evidence: Optional[Mapping[str, Any]] = None,
    strategy_lane: Optional[str] = None,
) -> dict[str, Any]:
    action = str(requested_action or "watch").lower()
    quality_status = str(quality_report.get("status") or "conditional")
    reasons: list[str] = []
    decision = action
    multiplier = 1.0

    if action in EXIT_ACTIONS and t1_block:
        decision = "hold_locked"
        multiplier = 0.0
        reasons.append(str(t1_block.get("error") or "A股T+1锁定"))

    if action in POSITIVE_ACTIONS:
        if quality_status == "rejected":
            decision = "avoid"
            multiplier = 0.0
            reasons.append("quality_rejected")
        elif quality_status != "passed":
            decision = "watch"
            multiplier = 0.0
            reasons.append("quality_not_passed")

        if strategy_record:
            research_allowed = strategy_record.get("allowed_in_live_agent")
            gating_status = strategy_record.get("gating_status", "enabled")
            if gating_status == "disabled" or research_allowed is False:
                decision = "avoid"
                multiplier = 0.0
                reasons.append("strategy_not_allowed")

        if (market_regime or {}).get("regime") == "risk_off" and decision in POSITIVE_ACTIONS:
            decision = "watch"
            multiplier = 0.0
            reasons.append("market_risk_off")

        if portfolio_risk and portfolio_risk.get("allowed") is False:
            decision = "avoid"
            multiplier = 0.0
            reasons.extend(str(reason) for reason in portfolio_risk.get("reasons") or [])

        serenity = (research_evidence or {}).get("serenity") or {}
        chanlun = (research_evidence or {}).get("chanlun") or {}
        if chanlun.get("live_bearish_signals"):
            decision = "avoid"
            multiplier = 0.0
            reasons.append("chanlun_live_bearish_signal")
        elif serenity.get("hard_risks"):
            decision = "avoid"
            multiplier = 0.0
            reasons.append("serenity_hard_risk")
        elif (
            strategy_lane == "trend"
            and serenity.get("available")
            and serenity.get("stale")
            and decision in POSITIVE_ACTIONS
        ):
            multiplier = min(multiplier, 0.5)
            reasons.append("serenity_stale_reduced")

    return {
        "schema": "a_share_decision_policy_v1",
        "requested_action": action,
        "decision": decision,
        "position_multiplier": multiplier,
        "quality_status": quality_status,
        "reasons": reasons,
        "strategy_lane": strategy_lane,
        "portfolio_risk": dict(portfolio_risk or {}),
        "research_evidence": dict(research_evidence or {}),
    }
