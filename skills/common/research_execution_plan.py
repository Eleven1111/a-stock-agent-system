"""Deterministic, fail-closed bridge from research to a paper execution plan.

The compiler accepts only a schema-valid supporting proposal that is either
bound to its synthesis artifact or covered by an independently hashed approval
artifact.  Every decision input is point-in-time bound.  The output remains a
review artifact: this module has no broker/order integration and always emits
``execution_eligible=False``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from decision_policy import evaluate_decision
from portfolio_policy import evaluate_candidate
from recommendation_quality import build_execution_plan
from state_store import atomic_write_json


SCHEMA = "research_execution_plan_v1"
PROPOSAL_SCHEMA = "research_proposal_v1"
SYNTHESIS_SCHEMA = "research_synthesis_v1"
APPROVAL_SCHEMA = "research_proposal_approval_v1"
CONTEXT_PIT_SCHEMA = "research_execution_context_pit_v1"
DEFAULT_PROPOSAL_MAX_AGE_SECONDS = 86_400
SHANGHAI = ZoneInfo("Asia/Shanghai")

# Paths and hashes written after the deterministic synthesis decision are not
# part of the synthesis identity.  This list is the shared canonical contract
# for producers and consumers; do not add business fields here.
SYNTHESIS_DERIVED_FIELDS = frozenset(
    {"proposal_path", "report_path", "synthesis_sha256"}
)


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def synthesis_core_sha256(synthesis: Mapping[str, Any]) -> str:
    core = {
        key: value
        for key, value in synthesis.items()
        if key not in SYNTHESIS_DERIVED_FIELDS
    }
    return canonical_sha256(core)


def _normalize_code(value: Any) -> str:
    code = str(value or "").strip().lower()
    if code.startswith(("sh", "sz")):
        code = code[2:]
    return code.zfill(6) if code.isdigit() else code


def _asof(value: Any) -> str:
    text = str(value or date.today().isoformat())
    return text[:10]


def _aware_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _proposal_temporal_reasons(
    proposal: Mapping[str, Any],
    *,
    current: datetime,
    max_age_seconds: int,
) -> tuple[list[str], datetime | None]:
    reasons: list[str] = []
    if not str(proposal.get("task_id") or "").strip():
        reasons.append("proposal_task_id_missing")
    if not _normalize_code((proposal.get("subject") or {}).get("code")):
        reasons.append("proposal_code_missing")

    raw_trading_date = str(proposal.get("trading_date") or "").strip()
    trading_day: date | None = None
    if not raw_trading_date:
        reasons.append("proposal_trading_date_missing")
    else:
        try:
            trading_day = date.fromisoformat(raw_trading_date)
        except ValueError:
            reasons.append("proposal_trading_date_invalid")

    raw_created_at = proposal.get("created_at")
    created_at: datetime | None = None
    if raw_created_at in (None, ""):
        reasons.append("proposal_created_at_missing")
    else:
        created_at = _aware_datetime(raw_created_at)
        if created_at is None:
            reasons.append("proposal_created_at_invalid")

    if created_at is not None:
        if created_at > current:
            reasons.append("proposal_future")
        elif (current - created_at).total_seconds() > max_age_seconds:
            reasons.append("proposal_stale")
        if trading_day is not None and trading_day != created_at.astimezone(
            SHANGHAI
        ).date():
            reasons.append("proposal_trading_date_mismatch")
    if trading_day is not None and trading_day != current.astimezone(SHANGHAI).date():
        reasons.append("proposal_trading_date_mismatch")
    return list(dict.fromkeys(reasons)), created_at


def _synthesis_temporal_reasons(
    synthesis: Any,
    *,
    proposal_created_at: datetime | None,
    current: datetime,
    max_age_seconds: int,
) -> list[str]:
    if not isinstance(synthesis, Mapping):
        return []
    generated_at = _aware_datetime(synthesis.get("generated_at"))
    if generated_at is None:
        return ["synthesis_generated_at_invalid"]
    reasons: list[str] = []
    if generated_at > current:
        reasons.append("synthesis_future")
    if proposal_created_at is not None and generated_at > proposal_created_at:
        reasons.append("synthesis_after_proposal")
    if generated_at <= current and (
        current - generated_at
    ).total_seconds() > max_age_seconds:
        reasons.append("synthesis_stale")
    return reasons


def _context_body(context: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in context.items() if key != "point_in_time"}


def _validate_context(
    name: str,
    context: Any,
    *,
    current: datetime,
) -> tuple[list[str], Mapping[str, Any] | None]:
    if not isinstance(context, Mapping):
        return [f"{name}_context_missing"], None
    pit = context.get("point_in_time")
    if not isinstance(pit, Mapping):
        return [f"{name}_context_pit_missing"], None

    reasons: list[str] = []
    if pit.get("schema") != CONTEXT_PIT_SCHEMA:
        reasons.append(f"{name}_context_pit_invalid")
    if not str(pit.get("ref") or "").strip():
        reasons.append(f"{name}_context_ref_missing")
    expected_hash = canonical_sha256(_context_body(context))
    if pit.get("sha256") != expected_hash:
        reasons.append(f"{name}_context_hash_mismatch")

    asof = _aware_datetime(pit.get("asof"))
    captured = _aware_datetime(pit.get("captured_at"))
    try:
        ttl_seconds = int(pit.get("ttl_seconds"))
    except (TypeError, ValueError):
        ttl_seconds = 0
    if asof is None or captured is None or ttl_seconds <= 0:
        reasons.append(f"{name}_context_pit_invalid")
    elif asof > captured or asof > current or captured > current:
        reasons.append(f"{name}_context_future")
    elif (current - captured).total_seconds() > ttl_seconds:
        reasons.append(f"{name}_context_stale")
    return list(dict.fromkeys(reasons)), pit


def _supporting_verdict(artifact: Mapping[str, Any]) -> bool:
    verdict = str(artifact.get("verdict") or "")
    return verdict == "advance" or (
        verdict == "adjudicated"
        and str(artifact.get("final_stance") or "") == "support"
    )


def _synthesis_verified(
    proposal: Mapping[str, Any],
    synthesis: Any,
) -> bool:
    if not isinstance(synthesis, Mapping):
        return False
    if synthesis.get("schema") != SYNTHESIS_SCHEMA:
        return False
    claimed = str(proposal.get("synthesis_sha256") or "")
    if not claimed or claimed != synthesis_core_sha256(synthesis):
        return False
    if synthesis.get("synthesis_sha256") not in (None, claimed):
        return False
    if synthesis.get("task_id") != proposal.get("task_id"):
        return False
    if synthesis.get("verdict") != proposal.get("verdict"):
        return False
    if str(synthesis.get("final_stance") or "") != str(
        proposal.get("final_stance") or ""
    ):
        return False
    proposal_code = _normalize_code((proposal.get("subject") or {}).get("code"))
    synthesis_code = _normalize_code((synthesis.get("subject") or {}).get("code"))
    if synthesis_code and synthesis_code != proposal_code:
        return False
    return _supporting_verdict(synthesis)


def _approval_verified(
    proposal: Mapping[str, Any],
    approval: Any,
    *,
    current: datetime,
    synthesis: Mapping[str, Any] | None = None,
) -> bool:
    if not isinstance(approval, Mapping):
        return False
    if (
        approval.get("schema") != APPROVAL_SCHEMA
        or approval.get("status") != "approved"
        or approval.get("task_id") != proposal.get("task_id")
        or not str(approval.get("reviewer") or "").strip()
        or approval.get("proposal_sha256") != canonical_sha256(proposal)
        or approval.get("synthesis_sha256") != proposal.get("synthesis_sha256")
    ):
        return False
    body = {key: value for key, value in approval.items() if key != "artifact_sha256"}
    if approval.get("artifact_sha256") != canonical_sha256(body):
        return False
    approved_at = _aware_datetime(approval.get("approved_at"))
    proposal_created_at = _aware_datetime(proposal.get("created_at"))
    if (
        approved_at is None
        or proposal_created_at is None
        or approved_at < proposal_created_at
        or approved_at > current
    ):
        return False
    if isinstance(synthesis, Mapping):
        synthesis_generated_at = _aware_datetime(synthesis.get("generated_at"))
        if (
            synthesis_generated_at is None
            or approved_at < synthesis_generated_at
        ):
            return False
    return True


def _fresh_quote_verified(
    market_context: Mapping[str, Any],
    *,
    market_reasons: list[str],
    current: datetime,
    max_age_seconds: int,
) -> bool:
    if market_reasons:
        return False
    pit = market_context.get("point_in_time") or {}
    captured = _aware_datetime(pit.get("captured_at"))
    try:
        price = float(market_context.get("price"))
        previous = float(market_context.get("prev_close"))
    except (TypeError, ValueError):
        return False
    if (
        captured is None
        or not _normalize_code(market_context.get("code"))
        or price <= 0
        or previous <= 0
        or (market_context.get("tradeability") or {}).get("tradeable") is not True
    ):
        return False
    return (current - captured).total_seconds() <= max(1, max_age_seconds)


def _t1_verified(
    *,
    market_context: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> bool:
    action = str(market_context.get("action") or "").lower()
    if action not in {"sell", "reduce"}:
        # An opening plan must explicitly defer its protective exit until T+1.
        return str(plan.get("horizon") or "").startswith("T+1")

    code = _normalize_code(market_context.get("code"))
    position = next(
        (
            item
            for item in (portfolio.get("positions") or [])
            if isinstance(item, Mapping)
            and _normalize_code(item.get("code")) == code
        ),
        None,
    )
    if not isinstance(position, Mapping):
        return False
    try:
        shares = float(position.get("shares") or 0)
        today_bought = float(position.get("today_bought_shares") or 0)
        available = float(position.get("available_shares"))
        requested = float(market_context.get("requested_shares") or available)
    except (TypeError, ValueError):
        return False
    settled = max(0.0, shares - today_bought)
    return requested > 0 and available >= requested and settled >= requested


def _configured_max_age(config: Mapping[str, Any]) -> int:
    try:
        value = int(
            config.get("proposal_max_age_seconds")
            or DEFAULT_PROPOSAL_MAX_AGE_SECONDS
        )
    except (TypeError, ValueError):
        return DEFAULT_PROPOSAL_MAX_AGE_SECONDS
    return value if value > 0 else DEFAULT_PROPOSAL_MAX_AGE_SECONDS


def _temporal_checks(
    *,
    proposal: Mapping[str, Any],
    synthesis_artifact: Mapping[str, Any] | None,
    current: datetime,
    config: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    max_age_seconds = _configured_max_age(config)
    proposal_reasons, proposal_created_at = _proposal_temporal_reasons(
        proposal,
        current=current,
        max_age_seconds=max_age_seconds,
    )
    synthesis_reasons = _synthesis_temporal_reasons(
        synthesis_artifact,
        proposal_created_at=proposal_created_at,
        current=current,
        max_age_seconds=max_age_seconds,
    )
    return proposal_reasons, synthesis_reasons


def _prepare_candidate(
    proposal: Mapping[str, Any],
    market_context: Mapping[str, Any],
) -> tuple[dict[str, Any], str, str, dict[str, Any], dict[str, Any]]:
    candidate = dict(market_context)
    proposal_code = _normalize_code((proposal.get("subject") or {}).get("code"))
    market_code = _normalize_code(candidate.get("code"))
    candidate["code"] = market_code
    candidate.setdefault("name", (proposal.get("subject") or {}).get("name"))
    quality = dict(candidate.get("quality_report") or {})
    strategy = dict(candidate.get("strategy_record") or {})
    return candidate, proposal_code, market_code, quality, strategy


def _validate_contexts(
    *,
    candidate: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    quality: Mapping[str, Any],
    strategy: Mapping[str, Any],
    current: datetime,
) -> tuple[dict[str, list[str]], dict[str, Mapping[str, Any] | None]]:
    reasons: dict[str, list[str]] = {}
    bindings: dict[str, Mapping[str, Any] | None] = {}
    for name, context in (
        ("market", candidate),
        ("portfolio", portfolio),
        ("quality", quality),
        ("strategy", strategy),
    ):
        reasons[name], bindings[name] = _validate_context(
            name, context, current=current,
        )
    return reasons, bindings


def _build_decision_outputs(
    *,
    proposal: Mapping[str, Any],
    candidate: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    quality: Mapping[str, Any],
    strategy: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    strategy_lane = candidate.get("strategy_lane")
    plan = build_execution_plan(
        candidate,
        quality,
        asof=_asof(candidate.get("asof") or proposal.get("trading_date")),
        stage=str(candidate.get("stage") or "open"),
        atr=candidate.get("atr"),
        strategy_lane=strategy_lane,
    )
    concentration = evaluate_candidate(
        portfolio, candidate, float(plan.get("position_pct") or 0),
    )
    policy_decision = evaluate_decision(
        requested_action=str(plan.get("decision") or "watch"),
        quality_report=quality,
        strategy_record=strategy or None,
        t1_block=candidate.get("t1_block"),
        market_regime=candidate.get("market_regime"),
        portfolio_risk=concentration,
        research_evidence=candidate.get("research_evidence"),
        strategy_lane=strategy_lane,
        market_crowding=candidate.get("market_crowding"),
        discipline_state=candidate.get("discipline_state"),
        raw_score=candidate.get("raw_score"),
    )
    return plan, concentration, policy_decision


def _verify_gates(
    *,
    proposal: Mapping[str, Any],
    synthesis_artifact: Mapping[str, Any] | None,
    approval_artifact: Mapping[str, Any] | None,
    synthesis_temporal_reasons: list[str],
    candidate: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    plan: Mapping[str, Any],
    market_reasons: list[str],
    current: datetime,
    config: Mapping[str, Any],
) -> dict[str, bool]:
    synthesis_verified = (
        _synthesis_verified(proposal, synthesis_artifact)
        and not synthesis_temporal_reasons
    )
    return {
        "synthesis": synthesis_verified,
        "approval": _approval_verified(
            proposal,
            approval_artifact,
            current=current,
            synthesis=synthesis_artifact,
        ),
        "quote": _fresh_quote_verified(
            candidate,
            market_reasons=market_reasons,
            current=current,
            max_age_seconds=int(
                config.get("fresh_quote_max_age_seconds") or 300
            ),
        ),
        "t1": _t1_verified(
            market_context=candidate,
            portfolio=portfolio,
            plan=plan,
        ),
    }


def _blocking_checks(
    *,
    proposal: Mapping[str, Any],
    candidate: Mapping[str, Any],
    quality: Mapping[str, Any],
    proposal_code: str,
    market_code: str,
    proposal_temporal_reasons: list[str],
    synthesis_temporal_reasons: list[str],
    context_reasons: Mapping[str, list[str]],
    gates: Mapping[str, bool],
    synthesis_supplied: bool,
    approval_supplied: bool,
    plan: Mapping[str, Any],
    concentration: Mapping[str, Any],
    policy_decision: Mapping[str, Any],
) -> list[str]:
    blocking: list[str] = []
    if proposal.get("schema") != PROPOSAL_SCHEMA:
        blocking.append("proposal_schema_invalid")
    if proposal.get("policy_gate_required") is not True:
        blocking.append("proposal_policy_gate_missing")
    if not _supporting_verdict(proposal):
        blocking.append("proposal_verdict_not_supporting")
    if not str(proposal.get("synthesis_ref") or "").strip():
        blocking.append("proposal_synthesis_ref_missing")
    blocking.extend(proposal_temporal_reasons)
    blocking.extend(synthesis_temporal_reasons)
    if approval_supplied and not gates["approval"]:
        blocking.append("approval_artifact_invalid")
    if synthesis_supplied and not gates["synthesis"]:
        blocking.append("synthesis_artifact_invalid")
    if not (gates["approval"] or gates["synthesis"]):
        blocking.append("proposal_not_approved_or_synthesis_verified")
    if proposal_code and market_code and proposal_code != market_code:
        blocking.append("proposal_market_code_mismatch")
    if not proposal_code or not market_code:
        blocking.append("code_missing")
    for name in ("market", "portfolio", "quality", "strategy"):
        blocking.extend(context_reasons[name])
    if not gates["quote"]:
        blocking.append("fresh_quote_not_verified")
    if not gates["t1"]:
        blocking.append("t1_locked")
    if quality.get("status") != "passed":
        blocking.append("quality_not_passed")
    if (candidate.get("tradeability") or {}).get("tradeable") is not True:
        blocking.append("not_tradeable")
    if concentration.get("allowed") is not True:
        blocking.extend(str(item) for item in concentration.get("reasons") or [])
    if plan.get("decision") not in {"buy", "conditional_buy"}:
        blocking.append(f"execution_decision_{plan.get('decision')}")
    if policy_decision.get("decision") not in {"buy", "conditional_buy"}:
        blocking.append(f"decision_policy_{policy_decision.get('decision')}")
    return list(dict.fromkeys(blocking))


def _policy_payload(
    *,
    quality: Mapping[str, Any],
    candidate: Mapping[str, Any],
    concentration: Mapping[str, Any],
    policy_decision: Mapping[str, Any],
    gates: Mapping[str, bool],
) -> dict[str, Any]:
    return {
        "schema": "research_execution_policy_v1",
        "quality_ready": quality.get("status") == "passed",
        "tradeable": (candidate.get("tradeability") or {}).get("tradeable") is True,
        "portfolio": concentration,
        "decision_policy": policy_decision,
        "fresh_quote_required": True,
        "fresh_quote_verified": gates["quote"],
        "t1_required": True,
        "t1_verified": gates["t1"],
        "proposal_approved": gates["approval"],
        "synthesis_verified": gates["synthesis"],
    }


def _result_payload(
    *,
    proposal: Mapping[str, Any],
    approval_artifact: Mapping[str, Any] | None,
    market_code: str,
    current: datetime,
    bindings: Mapping[str, Mapping[str, Any] | None],
    plan: Mapping[str, Any],
    policy: Mapping[str, Any],
    blocking: list[str],
) -> dict[str, Any]:
    result = {
        "schema": SCHEMA,
        "task_id": proposal.get("task_id"),
        "code": market_code,
        "trading_date": proposal.get("trading_date"),
        "compiled_at": current.isoformat(),
        "research_proposal_ref": proposal.get("task_id"),
        "input_bindings": {
            "proposal_sha256": canonical_sha256(proposal),
            "synthesis_ref": proposal.get("synthesis_ref"),
            "synthesis_sha256": proposal.get("synthesis_sha256"),
            "approval_sha256": (
                approval_artifact.get("artifact_sha256")
                if isinstance(approval_artifact, Mapping)
                else None
            ),
            "market_context": dict(bindings["market"] or {}),
            "portfolio_context": dict(bindings["portfolio"] or {}),
            "quality_context": dict(bindings["quality"] or {}),
            "strategy_context": dict(bindings["strategy"] or {}),
        },
        "plan": dict(plan),
        "policy": dict(policy),
        "blocking_checks": blocking,
        "policy_gate_required": True,
        "execution_eligible": False,
        "live_effect": "none_until_human_and_decision_policy_gate",
    }
    result["status"] = "ready_for_gate" if not blocking else "blocked"
    return result


def compile_execution_plan(
    proposal: Mapping[str, Any],
    *,
    market_context: Mapping[str, Any], portfolio: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
    synthesis_artifact: Mapping[str, Any] | None = None,
    approval_artifact: Mapping[str, Any] | None = None, now: str | None = None,
) -> dict[str, Any]:
    config = dict(config or {})
    current = _aware_datetime(now or datetime.now().astimezone().isoformat())
    if current is None:  # Defensive only; the generated default is always aware.
        raise ValueError("now must be a timezone-aware ISO timestamp")
    proposal_temporal_reasons, synthesis_temporal_reasons = _temporal_checks(
        proposal=proposal,
        synthesis_artifact=synthesis_artifact,
        current=current,
        config=config,
    )
    candidate, proposal_code, market_code, quality, strategy = _prepare_candidate(
        proposal, market_context,
    )
    context_reasons, bindings = _validate_contexts(
        candidate=candidate,
        portfolio=portfolio,
        quality=quality,
        strategy=strategy,
        current=current,
    )
    plan, concentration, policy_decision = _build_decision_outputs(
        proposal=proposal,
        candidate=candidate,
        portfolio=portfolio,
        quality=quality,
        strategy=strategy,
    )
    gates = _verify_gates(
        proposal=proposal,
        synthesis_artifact=synthesis_artifact,
        approval_artifact=approval_artifact,
        synthesis_temporal_reasons=synthesis_temporal_reasons,
        candidate=candidate,
        portfolio=portfolio,
        plan=plan,
        market_reasons=context_reasons["market"],
        current=current,
        config=config,
    )
    blocking = _blocking_checks(
        proposal=proposal,
        candidate=candidate,
        quality=quality,
        proposal_code=proposal_code,
        market_code=market_code,
        proposal_temporal_reasons=proposal_temporal_reasons,
        synthesis_temporal_reasons=synthesis_temporal_reasons,
        context_reasons=context_reasons,
        gates=gates,
        synthesis_supplied=synthesis_artifact is not None,
        approval_supplied=approval_artifact is not None,
        plan=plan,
        concentration=concentration,
        policy_decision=policy_decision,
    )
    policy = _policy_payload(
        quality=quality,
        candidate=candidate,
        concentration=concentration,
        policy_decision=policy_decision,
        gates=gates,
    )
    return _result_payload(
        proposal=proposal,
        approval_artifact=approval_artifact,
        market_code=market_code,
        current=current,
        bindings=bindings,
        plan=plan,
        policy=policy,
        blocking=blocking,
    )


def persist_execution_plan(path: str, result: Mapping[str, Any]) -> str:
    atomic_write_json(path, dict(result))
    return path
