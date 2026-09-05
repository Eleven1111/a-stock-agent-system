#!/usr/bin/env python3
"""Admission and execution for a *named*, frozen, exploratory paper experiment.

Two different things want to reach the paper account and they must not be
confused.  ``strategy_registry``'s ``paper_only`` promotion means "this strategy
passed the full research gate and was granted a supervised pilot"; that path is
untouched here, broker reconciliation and all.  An *exploratory* experiment has
passed no such gate: it exists to produce evidence, runs on its own account
scope, and says so in every artifact it writes.

The registry already computes ``paper_runtime_allowed`` and ``paper_live_weight``
but nothing consumed them, so "promoted" and "running" were only connected in
prose.  An experiment declaring ``entry_point="pilot_permission"`` now actually
reads them; one declaring ``entry_point="exploratory_scope"`` explicitly does
not, and is barred from claiming pilot status.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

import strategy_registry
from executable_forward_simulation import (
    STATUS_EXITED,
    STATUS_NOT_FILLED,
    simulate_executable_forward,
)
from research_artifact import json_sha256

SCHEMA = "exploratory_paper_experiment_v1"
RUN_SCHEMA = "exploratory_paper_experiment_run_v1"
ENGINE_VERSION = "exploratory-paper-experiment-v1"

ENTRY_POINT_PILOT = "pilot_permission"
ENTRY_POINT_EXPLORATORY = "exploratory_scope"
ENTRY_POINTS = (ENTRY_POINT_PILOT, ENTRY_POINT_EXPLORATORY)

#: ``paper_pilot_weight`` is capped by ``maximum_manual_pilot_weight`` in the
#: validation thresholds, i.e. it bounds the share of the account a pilot may
#: consume.  It is a portfolio budget fraction, never a per-name position size.
WEIGHT_SEMANTICS = "portfolio_budget_fraction"

REQUIRED_SPEC_FIELDS = (
    "experiment_id", "strategy_id", "strategy_rules_sha256", "sample_start",
    "entry_point", "account_scope", "signal_cutoff_rule", "earliest_entry_rule",
    "exit_rule", "hold_sessions", "budget", "ranking",
)


class ExperimentError(ValueError):
    """Raised when an experiment is malformed or not admitted."""


def freeze_experiment(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and hash an experiment definition before it may produce anything."""

    missing = [
        field for field in REQUIRED_SPEC_FIELDS
        if spec.get(field) is None or spec.get(field) == ""
    ]
    if missing:
        raise ExperimentError(f"experiment_spec_incomplete:{','.join(sorted(missing))}")
    if spec["entry_point"] not in ENTRY_POINTS:
        raise ExperimentError(f"unknown_entry_point:{spec['entry_point']}")
    if int(spec["hold_sessions"]) < 1:
        raise ExperimentError("hold_sessions must be at least 1: T+1 forbids a same-session exit")
    ranking = spec["ranking"]
    if not isinstance(ranking, Mapping) or not ranking.get("keys"):
        raise ExperimentError("ranking_keys_required")
    frozen = {
        "schema": SCHEMA,
        "engine_version": ENGINE_VERSION,
        "weight_semantics": WEIGHT_SEMANTICS,
        "research_only": True,
        "live_order_sent": False,
        "claims_research_gate_passed": spec["entry_point"] == ENTRY_POINT_PILOT,
        **{field: spec[field] for field in REQUIRED_SPEC_FIELDS},
    }
    frozen["experiment_sha256"] = json_sha256(frozen)
    return frozen


def scope_idempotency_key(
    experiment: Mapping[str, Any], event_type: str, asof: str, entity_id: str = ""
) -> str:
    """Idempotency key carrying the account and experiment scope.

    Without the scope two experiments evaluating the same code on the same day
    collide, and the second one's event is silently swallowed as a duplicate.
    """

    parts = [
        event_type, str(experiment["account_scope"]), str(experiment["experiment_id"]),
        str(experiment["experiment_sha256"])[:16], asof,
    ]
    if entity_id:
        parts.append(entity_id)
    return ":".join(parts)


def admit(
    experiment: Mapping[str, Any],
    *,
    registry_file: str | None = None,
    exploratory_scopes: Sequence[str] = (),
) -> dict[str, Any]:
    """Decide whether this experiment may write to its paper account scope."""

    entry_point = str(experiment.get("entry_point") or "")
    decision: dict[str, Any] = {
        "schema": "exploratory_paper_admission_v1",
        "experiment_id": experiment.get("experiment_id"),
        "experiment_sha256": experiment.get("experiment_sha256"),
        "account_scope": experiment.get("account_scope"),
        "entry_point": entry_point,
        "weight_semantics": WEIGHT_SEMANTICS,
    }
    if experiment.get("experiment_sha256") != json_sha256(
        {key: value for key, value in experiment.items() if key != "experiment_sha256"}
    ):
        return {**decision, "allowed": False, "reason": "experiment_hash_mismatch", "weight": 0.0}
    if entry_point == ENTRY_POINT_PILOT:
        strategy_id = str(experiment["strategy_id"])
        allowed = strategy_registry.paper_runtime_allowed(strategy_id, registry_file)
        weight = strategy_registry.paper_live_weight(strategy_id, registry_file)
        return {
            **decision,
            "allowed": bool(allowed and weight > 0),
            "weight": weight,
            "reason": "paper_pilot_permission" if allowed and weight > 0
            else "paper_runtime_not_permitted",
        }
    if str(experiment["account_scope"]) not in set(exploratory_scopes):
        return {
            **decision, "allowed": False, "weight": 0.0,
            "reason": "exploratory_scope_not_registered",
        }
    return {
        **decision, "allowed": True,
        "weight": float((experiment.get("budget") or {}).get("account_fraction") or 0.0),
        "reason": "exploratory_scope_registered",
        # An exploratory scope is evidence collection, not an approval.
        "research_gate_passed": False,
    }


def rank_candidates(
    experiment: Mapping[str, Any], candidates: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Order candidates by the ranking frozen into the experiment.

    The final tie-break is ``entity_id``, which is pre-registered and carries no
    outcome information -- unlike relying on whatever order the upstream response
    happened to arrive in.
    """

    ranking = experiment["ranking"]
    keys = [(str(item["field"]), str(item.get("direction") or "desc")) for item in ranking["keys"]]

    def sort_key(candidate: Mapping[str, Any]) -> tuple:
        values: list[Any] = []
        for field, direction in keys:
            raw = candidate.get(field)
            try:
                number = float(raw)
            except (TypeError, ValueError):
                number = float("-inf")
            values.append(-number if direction == "desc" else number)
        values.append(str(candidate.get("entity_id") or ""))
        return tuple(values)

    return sorted((dict(item) for item in candidates), key=sort_key)


def select_within_budget(
    experiment: Mapping[str, Any], ranked: Sequence[Mapping[str, Any]], *, account_equity: float
) -> dict[str, Any]:
    """Split ranked candidates into admitted and budget-rejected, keeping both."""

    budget = experiment["budget"]
    fraction = float(budget.get("account_fraction") or 0.0)
    per_name = float(budget.get("max_position_fraction") or fraction)
    maximum_positions = int(budget.get("max_positions") or 0)
    total_notional = max(0.0, account_equity * fraction)
    per_name_notional = max(0.0, account_equity * min(per_name, fraction))
    admitted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    spent = 0.0
    for candidate in ranked:
        if maximum_positions and len(admitted) >= maximum_positions:
            rejected.append({**candidate, "reason": "max_positions_reached"})
            continue
        notional = min(per_name_notional, total_notional - spent)
        if notional <= 0:
            rejected.append({**candidate, "reason": "budget_exhausted"})
            continue
        admitted.append({**candidate, "order_amount": notional})
        spent += notional
    return {
        "admitted": admitted, "rejected": rejected,
        "total_notional": total_notional, "allocated_notional": spent,
    }


def simulate_admitted(
    experiment: Mapping[str, Any],
    admitted: Sequence[Mapping[str, Any]],
    bars_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Run each admitted candidate through the shared executable simulation."""

    results = []
    for candidate in admitted:
        code = str(candidate["entity_id"])
        results.append({
            "entity_id": code,
            "order_amount": candidate.get("order_amount"),
            **simulate_executable_forward(
                {
                    "decision_id": candidate.get("decision_id"),
                    "strategy_id": experiment["strategy_id"],
                    "entity_id": code,
                    "decision_date": candidate["decision_date"],
                    "observed_at": candidate["observed_at"],
                },
                bars_by_code.get(code) or [],
                hold_sessions=int(experiment["hold_sessions"]),
                order_amount=candidate.get("order_amount"),
                prev_close=candidate.get("prev_close"),
            ),
        })
    return results


def summarise_run(
    experiment: Mapping[str, Any],
    admission: Mapping[str, Any],
    selection: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    *,
    asof: str,
) -> dict[str, Any]:
    """Run record that keeps rejections and unresolved cases in the denominator."""

    by_status: dict[str, int] = {}
    for result in results:
        status = str(result.get("status"))
        by_status[status] = by_status.get(status, 0) + 1
    filled = [item for item in results if item.get("status") == STATUS_EXITED]
    record = {
        "schema": RUN_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "asof": asof,
        "experiment_id": experiment["experiment_id"],
        "experiment_sha256": experiment["experiment_sha256"],
        "account_scope": experiment["account_scope"],
        "entry_point": experiment["entry_point"],
        "admission": dict(admission),
        "considered": len(selection["admitted"]) + len(selection["rejected"]),
        "budget_rejected": len(selection["rejected"]),
        "budget_rejections": list(selection["rejected"]),
        "results": list(results),
        "status_counts": by_status,
        "unfilled": by_status.get(STATUS_NOT_FILLED, 0),
        "realised_net_returns": [item["net_return"] for item in filled],
        "research_only": True,
        "live_order_sent": False,
        "execution_evidence": bool(filled),
    }
    if not results:
        record["status"] = "no_eligible_evidence"
    else:
        record["status"] = "ok"
    record["run_sha256"] = json_sha256(
        {key: value for key, value in record.items() if key != "run_sha256"}
    )
    return record


__all__ = [
    "ENGINE_VERSION", "ENTRY_POINTS", "ENTRY_POINT_EXPLORATORY", "ENTRY_POINT_PILOT",
    "ExperimentError", "RUN_SCHEMA", "SCHEMA", "WEIGHT_SEMANTICS", "admit",
    "freeze_experiment", "rank_candidates", "scope_idempotency_key",
    "select_within_budget", "simulate_admitted", "summarise_run",
]
