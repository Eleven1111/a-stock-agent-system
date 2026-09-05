#!/usr/bin/env python3
"""Names for the three different things a "forward return" can mean here.

A price-path prediction label, a simulated executable result, and a manually
recorded real fill are three different measurements.  They share units and read
alike in a report, which is exactly why they need separate names and separate
gates: the settled forward label buys at the next session's open and sells at a
close that may be the *same* session, which no cash A-share account can do.
That label is a legitimate measurement of a price path.  It is not evidence that
the strategy can be traded.
"""

from __future__ import annotations

from typing import Any, Mapping

SCHEMA = "forward_label_descriptor_v1"

#: Entry at a reference price, exit after ``horizon`` sessions, no fill model,
#: no T+1 constraint.  Answers "did the price move?".
LABEL_PRICE_PATH = "price_path_prediction"

#: Entry and exit both passed a fill model and the T+1 rule measured from the
#: actual buy session.  Answers "could this position have been held and closed?".
LABEL_EXECUTABLE = "executable_simulated_result"

#: A fill a human actually recorded from a broker statement.
LABEL_MANUAL_FILL = "manual_recorded_fill"

LABEL_KINDS = (LABEL_PRICE_PATH, LABEL_EXECUTABLE, LABEL_MANUAL_FILL)


class LabelKindError(ValueError):
    """Raised when a label is offered as evidence it cannot support."""


def describe_price_path_label(
    record: Mapping[str, Any], policy: Mapping[str, Any]
) -> dict[str, Any]:
    """The research clock behind a settled forward sample, stated outright.

    Every field here is something a consumer would otherwise have to infer from
    a column name.  ``earliest_executable_entry`` is deliberately separate from
    ``reference_entry_date``: they coincide today, but the label's exit does not
    respect T+1 from either of them.
    """

    horizon = int(record.get("horizon_sessions") or record.get("primary_horizon") or 1)
    return {
        "schema": SCHEMA,
        "label_kind": LABEL_PRICE_PATH,
        "signal_cutoff": record.get("decision_date"),
        "signal_available_at": record.get("observed_at") or record.get("decision_available_at"),
        "reference_entry_rule": policy.get("entry_rule"),
        "reference_entry_date": record.get("entry_date"),
        "earliest_executable_entry": record.get("entry_date"),
        "exit_rule": f"close_of_session_{horizon}_after_reference_entry",
        "horizon_sessions": horizon,
        "respects_t_plus_one_from_entry": horizon > 1,
        "cost_basis": "modelled_assumption",
        "fill_model_applied": False,
        "execution_evidence": False,
        "applicable_scope": "price_direction_research_only",
    }


def assert_execution_evidence(descriptor: Mapping[str, Any]) -> None:
    """Fail closed unless the label actually measured an executable result.

    Call this at any gate whose question is "can this be traded?".  A gate whose
    question is "did the price move?" should not call it.
    """

    kind = str(descriptor.get("label_kind") or "")
    if kind not in LABEL_KINDS:
        raise LabelKindError(f"unknown_label_kind:{kind or 'missing'}")
    if kind == LABEL_PRICE_PATH:
        raise LabelKindError("price_path_label_is_not_execution_evidence")
    if kind == LABEL_EXECUTABLE and descriptor.get("execution_evidence") is not True:
        raise LabelKindError("executable_label_missing_execution_evidence_flag")


__all__ = [
    "LABEL_EXECUTABLE", "LABEL_KINDS", "LABEL_MANUAL_FILL", "LABEL_PRICE_PATH",
    "LabelKindError", "SCHEMA", "assert_execution_evidence",
    "describe_price_path_label",
]
