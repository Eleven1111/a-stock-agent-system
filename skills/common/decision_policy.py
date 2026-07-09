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
STATE_RISK_OFF_MULTIPLIER = 0.2
# S6 退潮/级联是比拥挤更明确的危险态，对 trend lane 同样有效；S0 冰点已由温度
# allow_new_daban 上游门控，这里不重复。
STATE_RISK_OFF_DOWNGRADE = {"S6"}


def _crowding_guard_enforced() -> bool:
    return os.environ.get("HERMES_CROWDING_GUARD", "observe").strip().lower() == "enforce"


# guardrail 结构化口径：把 reasons 里的机器码归入稳定的分组码，供审计/回流复用。
# 归类基于当前 decision_policy/recommendation_quality/trading_discipline 实际产生的
# reason token；未识别的新 token 落到 other，不阻断也不静默丢弃。
GUARDRAIL_REASON_CODES: dict[str, str] = {
    # 公告/研究类硬风险与情报缺口
    "announcement_scan_missing": "announcement_risk",
    "announcement_thesis_invalidated": "announcement_risk",
    "announcement_review_required": "announcement_risk",
    "announcement_hard_risk": "announcement_risk",
    "market_intelligence_missing": "announcement_risk",
    "market_intelligence_incomplete": "announcement_risk",
    "market_intelligence_hard_risk": "announcement_risk",
    "market_intelligence_not_ready": "announcement_risk",
    "chanlun_live_bearish_signal": "announcement_risk",
    "serenity_hard_risk": "announcement_risk",
    "serenity_stale_reduced": "announcement_risk",
    # 可成交性
    "not_tradeable": "tradeability",
    "required_fields_missing": "tradeability",
    # 组合集中度
    "single_position_limit": "concentration",
    "sector_exposure_limit": "concentration",
    "portfolio_value_unavailable": "concentration",
    # 情绪/温度/拥挤度门禁
    "market_risk_off": "temperature_gate",
    "market_state_ebbing_reduced": "temperature_gate",
    "crowding_climax_reduced": "temperature_gate",
    "crowding_climax_observed": "temperature_gate",
    # 打板纪律熔断
    "day_loss_stop": "discipline_gate",
    "week_trade_cap": "discipline_gate",
    "week_loss_freeze": "discipline_gate",
    "consecutive_losses_freeze": "discipline_gate",
    # 策略研究闸门/质检状态
    "quality_rejected": "strategy_gate",
    "quality_not_passed": "strategy_gate",
    "strategy_unverified": "strategy_gate",
    "strategy_not_allowed": "strategy_gate",
}


def _guardrail_reason_code(reason: str) -> str:
    if reason in GUARDRAIL_REASON_CODES:
        return GUARDRAIL_REASON_CODES[reason]
    if reason == "T1_LOCKED" or "T+1" in reason:
        return "t_plus_1"
    return "other"


def _build_guardrail(
    *,
    requested_action: str,
    decision: str,
    reasons: list[str],
    raw_score: Optional[float],
) -> Optional[dict[str, Any]]:
    """结构化解释 raw_action(评分建议动作) 与 final_action(实际输出动作) 的背离。

    只有二者不一致时才附带 guardrail；一致时返回 None，不强加噪音字段。
    """
    if decision == requested_action:
        return None
    return {
        "schema": "a_share_guardrail_v1",
        "raw_action": requested_action,
        "final_action": decision,
        "raw_score": raw_score,
        "reasons": [
            {"code": _guardrail_reason_code(reason), "detail": reason}
            for reason in reasons
        ],
    }


def _score(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# 市场状态 → (continue, constructive_divergence, collapse) 情景基准概率(报告 7.3)。
# 启发式映射, 非校准模型; 高脆弱时向 collapse 倾斜。
SCENARIO_BASE = {
    "S0": (0.15, 0.25, 0.60), "S1": (0.45, 0.35, 0.20),
    "S2": (0.60, 0.25, 0.15), "S3": (0.55, 0.30, 0.15),
    "S4": (0.35, 0.30, 0.35), "S5": (0.30, 0.45, 0.25),
    "S6": (0.10, 0.20, 0.70),
}


def _expected_paths(market_crowding: Optional[Mapping[str, Any]]) -> Optional[list[dict[str, Any]]]:
    """三情景分布。无市场状态证据时返回 None(不臆造情景)。"""
    if not isinstance(market_crowding, Mapping):
        return None
    base = SCENARIO_BASE.get(str(market_crowding.get("dominant_state") or ""))
    if base is None:
        return None
    cont, div, coll = base
    fragility = _score(market_crowding.get("fragility_score"))
    if fragility is not None and fragility >= FRAGILITY_CLIMAX_THRESHOLD:
        shift = 0.15
        cont, coll = max(0.0, cont - shift), coll + shift
    total = cont + div + coll
    return [
        {"scenario": "continue", "prob": round(cont / total, 4)},
        {"scenario": "constructive_divergence", "prob": round(div / total, 4)},
        {"scenario": "collapse", "prob": round(coll / total, 4)},
    ]


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
    discipline_state: Optional[Mapping[str, Any]] = None,
    raw_score: Optional[float] = None,
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

        if strategy_lane == "daban" and discipline_state and discipline_state.get("blocked"):
            decision = "avoid"
            multiplier = 0.0
            reasons.extend(str(reason) for reason in discipline_state.get("reasons") or [])

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
            if ebbing:
                multiplier = min(multiplier, STATE_RISK_OFF_MULTIPLIER)
                reasons.append("market_state_ebbing_reduced")
            elif climax:
                if _crowding_guard_enforced():
                    multiplier = min(multiplier, CROWDING_CLIMAX_MULTIPLIER)
                    reasons.append("crowding_climax_reduced")
                else:
                    reasons.append("crowding_climax_observed")

    return {
        "schema": "a_share_decision_policy_v1",
        "requested_action": action,
        "decision": decision,
        "position_multiplier": multiplier,
        "quality_status": quality_status,
        "reasons": reasons,
        "strategy_lane": strategy_lane,
        "portfolio_risk": dict(portfolio_risk or {}),
        "discipline_state": dict(discipline_state or {}),
        "research_evidence": dict(research_evidence or {}),
        "market_crowding": dict(market_crowding or {}),
        "expected_paths": _expected_paths(market_crowding),
        "expected_paths_calibrated": False,
        "abstain": decision == "watch",
        "guardrail": _build_guardrail(
            requested_action=action,
            decision=decision,
            reasons=reasons,
            raw_score=raw_score,
        ),
    }
