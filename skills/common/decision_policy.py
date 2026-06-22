"""Single deterministic policy gate for recommendations from any runtime."""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional


POSITIVE_ACTIONS = {"buy", "add", "conditional_buy"}
EXIT_ACTIONS = {"sell", "reduce"}

# 拥挤/脆弱"高潮"阈值（对齐 crowding_fragility.signal_thresholds）。降暴露的保守方向，
# 与 market_risk_off 同类，不构成"未过研究闸门的正向策略上线"。横截面预警是非确定性
# 证据，故默认 observe（只记录不降级）；HERMES_CROWDING_GUARD=enforce 才温和减半。
CROWDING_CLIMAX_THRESHOLD = 0.60
FRAGILITY_CLIMAX_THRESHOLD = 0.55
CROWDING_CLIMAX_MULTIPLIER = 0.5
# S6 退潮/级联是比拥挤更明确的危险态，对 trend lane 同样有效；S0 冰点已由温度
# allow_new_daban 上游门控，这里不重复。
STATE_RISK_OFF_DOWNGRADE = {"S6"}


def _crowding_guard_enforced() -> bool:
    return os.environ.get("HERMES_CROWDING_GUARD", "observe").strip().lower() == "enforce"


def _score(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    market_crowding: Optional[Mapping[str, Any]] = None,
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

        if strategy_record is None:
            if decision in POSITIVE_ACTIONS:
                decision = "watch"
            multiplier = 0.0
            reasons.append("strategy_unverified")
        elif strategy_record.get("runtime_allowed") is not True:
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
        market_intelligence = (
            (research_evidence or {}).get("market_intelligence") or {}
        )
        if chanlun.get("live_bearish_signals"):
            decision = "avoid"
            multiplier = 0.0
            reasons.append("chanlun_live_bearish_signal")
        elif market_intelligence.get("hard_risks"):
            decision = "avoid"
            multiplier = 0.0
            reasons.append("market_intelligence_hard_risk")
        elif (
            research_evidence is not None
            and "market_intelligence" in research_evidence
            and (
                not market_intelligence.get("available")
                or market_intelligence.get("directional_ready") is not True
            )
            and decision in POSITIVE_ACTIONS
        ):
            decision = "watch"
            multiplier = 0.0
            reasons.append("market_intelligence_not_ready")
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

        if decision in POSITIVE_ACTIONS and isinstance(market_crowding, Mapping):
            crowding = _score(market_crowding.get("crowding_score"))
            fragility = _score(market_crowding.get("fragility_score"))
            climax = (
                crowding is not None
                and fragility is not None
                and crowding >= CROWDING_CLIMAX_THRESHOLD
                and fragility >= FRAGILITY_CLIMAX_THRESHOLD
            )
            ebbing = str(market_crowding.get("dominant_state") or "") in STATE_RISK_OFF_DOWNGRADE
            if climax or ebbing:
                tag = "crowding_climax" if climax else "market_state_ebbing"
                if _crowding_guard_enforced():
                    multiplier = min(multiplier, CROWDING_CLIMAX_MULTIPLIER)
                    reasons.append(f"{tag}_reduced")
                else:
                    reasons.append(f"{tag}_observed")

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
        "market_crowding": dict(market_crowding or {}),
    }
