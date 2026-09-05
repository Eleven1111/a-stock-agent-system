#!/usr/bin/env python3
"""Classify the gates, and account for what each one actually did.

Three kinds of gate get talked about as if they were one:

* **hard execution constraints** -- T+1, cash, real fillability, permissions,
  timing, required-data completeness.  Every control arm keeps these; ablating
  them measures nothing real.
* **unproven strategy filters** -- MFI, Chanlun, empirical score thresholds,
  regime filters.  These are candidates for ablation, inside an isolated
  experiment only.
* **explanatory evidence** -- things that never changed a ranking or a decision.
  Recording their purpose is fine; counting them as risk control is not.

This module builds the inventory and the per-gate accounting.  It deliberately
does **not** change any production rule: the first deliverable is a list, and
which filters survive is a research decision made later, on evidence.

The accounting keeps rejections in the denominator.  "It was rejected and then
went up" is not automatically a miss either -- whether it could have been bought,
whether the cash was already committed, and the holding period all have to be
part of the answer, so those counts are carried alongside.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

SCHEMA = "gate_inventory_v1"
ACCOUNTING_SCHEMA = "gate_accounting_v1"

KIND_HARD = "hard_execution_constraint"
KIND_UNPROVEN = "unproven_strategy_filter"
KIND_EXPLANATORY = "explanatory_evidence"
GATE_KINDS = (KIND_HARD, KIND_UNPROVEN, KIND_EXPLANATORY)

#: Counts every unproven gate has to be able to answer before anyone argues it
#: earns its place.  A gate that cannot fill these in has not been measured.
REQUIRED_COUNTS = (
    "candidates_before_gate",
    "blocked_by_rule",
    "blocked_by_missing_data",
    "blocked_by_late_arrival",
    "rejected_at_execution",
    "terminal_outcomes",
    "unresolved",
)

#: Same-cohort deltas the gate has to move to be worth keeping.
REQUIRED_DELTAS = ("net_return", "max_drawdown", "turnover", "capital_exposure")


class GateInventoryError(ValueError):
    """Raised when a gate record cannot support the claim being made about it."""


def classify(gate: Mapping[str, Any]) -> str:
    """The kind a gate declares, validated against the closed set."""

    kind = str(gate.get("kind") or "")
    if kind not in GATE_KINDS:
        raise GateInventoryError(f"unknown_gate_kind:{kind or 'missing'}")
    return kind


def build_inventory(gates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """List the gates by kind without touching a single production rule."""

    rows = []
    for gate in gates:
        kind = classify(gate)
        rows.append({
            "gate_id": str(gate["gate_id"]),
            "kind": kind,
            "module": gate.get("module"),
            "ablatable": kind == KIND_UNPROVEN,
            "kept_in_every_arm": kind == KIND_HARD,
            "affects_ranking": bool(gate.get("affects_ranking", kind != KIND_EXPLANATORY)),
            "note": gate.get("note") or "",
        })
    rows.sort(key=lambda row: row["gate_id"])
    counts: dict[str, int] = {kind: 0 for kind in GATE_KINDS}
    for row in rows:
        counts[row["kind"]] += 1
    explanatory_claiming_control = [
        row["gate_id"] for row in rows
        if row["kind"] == KIND_EXPLANATORY and row["affects_ranking"]
    ]
    return {
        "schema": SCHEMA,
        "gates": rows,
        "counts_by_kind": counts,
        "ablatable_gates": [row["gate_id"] for row in rows if row["ablatable"]],
        # An explanatory gate that turns out to move the ranking was misfiled.
        "misfiled_explanatory_gates": explanatory_claiming_control,
        "production_rules_changed": False,
    }


def account_for_gate(gate_id: str, counts: Mapping[str, Any]) -> dict[str, Any]:
    """Per-gate accounting where nothing disappears from the denominator."""

    missing = [name for name in REQUIRED_COUNTS if counts.get(name) is None]
    if missing:
        raise GateInventoryError(f"gate_counts_incomplete:{','.join(sorted(missing))}")
    resolved = {name: int(counts[name]) for name in REQUIRED_COUNTS}
    before = resolved["candidates_before_gate"]
    accounted = (
        resolved["blocked_by_rule"] + resolved["blocked_by_missing_data"]
        + resolved["blocked_by_late_arrival"] + resolved["rejected_at_execution"]
        + resolved["terminal_outcomes"] + resolved["unresolved"]
    )
    return {
        "schema": ACCOUNTING_SCHEMA,
        "gate_id": gate_id,
        "counts": resolved,
        "accounted": accounted,
        # Every candidate that entered has to come out somewhere.
        "denominator_balanced": accounted == before,
        "unaccounted": before - accounted,
    }


def evaluate_gate_contribution(
    gate_id: str,
    accounting: Mapping[str, Any],
    deltas: Mapping[str, Any],
    *,
    cohort_id: str,
) -> dict[str, Any]:
    """Same-cohort contribution, refusing to conclude on an unbalanced ledger."""

    if not accounting.get("denominator_balanced"):
        return {
            "schema": "gate_contribution_v1", "gate_id": gate_id, "cohort_id": cohort_id,
            "status": "not_evaluated", "reason": "denominator_unbalanced",
            "unaccounted": accounting.get("unaccounted"),
        }
    missing = [name for name in REQUIRED_DELTAS if deltas.get(name) is None]
    if missing:
        return {
            "schema": "gate_contribution_v1", "gate_id": gate_id, "cohort_id": cohort_id,
            "status": "not_evaluated", "reason": "deltas_incomplete",
            "missing_deltas": sorted(missing),
        }
    return {
        "schema": "gate_contribution_v1",
        "gate_id": gate_id,
        "cohort_id": cohort_id,
        "status": "evaluated",
        "counts": accounting["counts"],
        "deltas": {name: float(deltas[name]) for name in REQUIRED_DELTAS},
        "rejected_but_rose": deltas.get("rejected_but_rose"),
        # A rejected name that later rose is only a miss if it was buyable, the
        # cash was free, and the holding period matches.
        "miss_requires": ["was_fillable", "capital_available", "holding_period_matched"],
        "research_only": True,
        "production_rule_changed": False,
    }


__all__ = [
    "ACCOUNTING_SCHEMA", "GATE_KINDS", "GateInventoryError", "KIND_EXPLANATORY",
    "KIND_HARD", "KIND_UNPROVEN", "REQUIRED_COUNTS", "REQUIRED_DELTAS", "SCHEMA",
    "account_for_gate", "build_inventory", "classify", "evaluate_gate_contribution",
]
