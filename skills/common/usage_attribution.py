#!/usr/bin/env python3
"""Tie host usage records to business runs, without building a billing system.

Everything here composes records that already exist: OpenClaw's own session and
usage entries on one side, this repository's task / role / run identifiers on the
other.  No provider is called, no price list is shipped, no second accounting
store is introduced.

Two habits this enforces:

* A missing price is ``unknown``, never ``0``.  Zero is a measurement; absence is
  not, and cost-per-adopted-result is undefined rather than zero when nothing was
  adopted.
* One host run is counted once.  A parent turn and its sub-tasks must not both be
  summed into the same total, so a record whose parent is also present is folded
  into the parent instead of added beside it.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

SCHEMA = "usage_attribution_v1"
UNKNOWN = "unknown"

MODULE_METRICS = (
    "planned_occurrences",
    "completed_valid",
    "on_time_valid",
    "consumed",
    "adopted",
)

CONSUMPTION_KINDS = ("display", "decision_support", "experiment", "incident_diagnosis")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def deduplicate_usage(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Drop child records whose parent run is also present.

    A host that reports both a parent turn's rollup and each sub-task will double
    the token count of every task that spawned one.
    """

    by_id = {str(row.get("run_id")): dict(row) for row in records if row.get("run_id")}
    folded = [
        run_id for run_id, row in by_id.items()
        if str(row.get("parent_run_id") or "") in by_id
    ]
    kept = [row for run_id, row in sorted(by_id.items()) if run_id not in set(folded)]
    return {
        "kept": kept,
        "folded_into_parent": sorted(folded),
        "input_records": len(records),
        "duplicate_ids": sorted(
            run_id for run_id in by_id
            if sum(1 for row in records if str(row.get("run_id")) == run_id) > 1
        ),
    }


def attribute_runs(
    usage_records: Sequence[Mapping[str, Any]],
    business_runs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind host usage to business task/role/run ids using the existing fields."""

    deduplicated = deduplicate_usage(usage_records)
    by_correlation = {
        str(run.get("correlation_id")): run
        for run in business_runs
        if run.get("correlation_id")
    }
    bound: list[dict[str, Any]] = []
    unbound: list[str] = []
    for row in deduplicated["kept"]:
        correlation = str(row.get("correlation_id") or "")
        business = by_correlation.get(correlation)
        if business is None:
            unbound.append(str(row.get("run_id")))
            continue
        bound.append({
            "run_id": row.get("run_id"),
            "correlation_id": correlation,
            "task_id": business.get("task_id"),
            "role": business.get("role"),
            "job_id": business.get("job_id"),
            "input_tokens": _number(row.get("input_tokens")),
            "output_tokens": _number(row.get("output_tokens")),
            "cache_tokens": _number(row.get("cache_tokens")),
            "billed_amount": _number(row.get("billed_amount")),
        })
    should_bind = len(deduplicated["kept"])
    return {
        "schema": SCHEMA,
        "bound": bound,
        "unbound_run_ids": sorted(unbound),
        "folded_into_parent": deduplicated["folded_into_parent"],
        "attribution_coverage": (
            round(len(bound) / should_bind, 4) if should_bind else None
        ),
        "attributable_runs": should_bind,
    }


def summarise_cost(
    attribution: Mapping[str, Any], *, adopted_results: int
) -> dict[str, Any]:
    """Cost totals that refuse to turn an absent price into a zero."""

    bound = attribution.get("bound") or []
    billed = [row["billed_amount"] for row in bound if row.get("billed_amount") is not None]
    priced = len(billed)
    tokens = {
        name: sum(row[name] or 0.0 for row in bound)
        for name in ("input_tokens", "output_tokens", "cache_tokens")
    }
    total_cost: Any = round(sum(billed), 6) if priced == len(bound) and bound else UNKNOWN
    if total_cost == UNKNOWN:
        cost_per_adopted: Any = UNKNOWN
    elif adopted_results <= 0:
        # Not zero: dividing by no adopted result has no value, it has no meaning.
        cost_per_adopted = "undefined"
    else:
        cost_per_adopted = round(float(total_cost) / adopted_results, 6)
    return {
        "schema": "usage_cost_summary_v1",
        "tokens": tokens,
        "priced_runs": priced,
        "unpriced_runs": len(bound) - priced,
        "billed_total": total_cost,
        "cost_basis": "actual_billing" if total_cost != UNKNOWN else UNKNOWN,
        "adopted_results": adopted_results,
        "cost_per_adopted_result": cost_per_adopted,
        "attribution_coverage": attribution.get("attribution_coverage"),
        "non_model_cost": UNKNOWN,
        "non_model_cost_note": "cpu, data fetch and io are not measured here",
    }


def module_effectiveness(
    module_id: str, metrics: Mapping[str, Any], *, latencies: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Per-module effectiveness with rates that stay undefined on a zero base."""

    resolved = {name: _number(metrics.get(name)) for name in MODULE_METRICS}
    missing = sorted(name for name, value in resolved.items() if value is None)
    planned = resolved["planned_occurrences"] or 0.0

    def _rate(numerator: float | None) -> Any:
        if numerator is None or planned <= 0:
            return UNKNOWN
        return round(numerator / planned, 4)

    return {
        "schema": "module_effectiveness_v1",
        "module_id": module_id,
        "counts": {name: resolved[name] for name in MODULE_METRICS},
        "missing_metrics": missing,
        "completed_valid_rate": _rate(resolved["completed_valid"]),
        "on_time_valid_rate": _rate(resolved["on_time_valid"]),
        "consumption_by_kind": {
            kind: metrics.get(f"consumed_{kind}") for kind in CONSUMPTION_KINDS
        },
        "adoption_rate": _rate(resolved["adopted"]),
        "latency_percentiles": dict(latencies or {}),
        # A module nobody consumed for a month is a candidate for review, not an
        # automatic shutdown: incident diagnosis, rare risk events, evidence
        # warm-up and audit retention are all real uses.
        "retirement_decision": "requires_dependency_closure_and_owner_review",
    }


def deterministic_module_costs(module_id: str) -> dict[str, Any]:
    """A command job with no model call has zero *model* tokens, not zero cost."""

    return {
        "schema": "module_effectiveness_v1",
        "module_id": module_id,
        "model_tokens": {"input_tokens": 0, "output_tokens": 0, "cache_tokens": 0},
        "model_token_basis": "no_model_call_in_this_job",
        "cpu_cost": UNKNOWN,
        "data_fetch_cost": UNKNOWN,
        "io_cost": UNKNOWN,
    }


__all__ = [
    "CONSUMPTION_KINDS", "MODULE_METRICS", "SCHEMA", "UNKNOWN", "attribute_runs",
    "deduplicate_usage", "deterministic_module_costs", "module_effectiveness",
    "summarise_cost",
]
