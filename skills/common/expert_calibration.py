"""Research-only calibration for committee roles.

This module measures directional committee stances against settled outcomes. It
does not mutate strategy weights, expert prompts, or live policy. A separate
registry is used for review recommendations so calibration cannot silently
become an execution control plane.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import math
from typing import Any, Mapping, Sequence

from paths import data_file
from state_store import atomic_write_json


SCHEMA = "expert_calibration_report_v1"
REGISTRY_SCHEMA = "expert_calibration_registry_v1"
JOIN_FIELDS = ("task_id", "code", "decision_date")
LINEAGE_FIELDS = ("dataset_id", "batch_id")
METRIC_DEFINITIONS = {
    "accuracy": "(true_positive + true_negative) / settled",
    "false_positive_rate": (
        "false_positive / (false_positive + true_negative)"
    ),
    "false_negative_rate": (
        "false_negative / (false_negative + true_positive)"
    ),
    "precision": "true_positive / (true_positive + false_positive)",
    "recall": "true_positive / (true_positive + false_negative)",
}


class CalibrationDataError(ValueError):
    """Raised when calibration inputs violate point-in-time data contracts."""


def _outcome_label(value: Any, threshold: float) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number > threshold:
        return "support"
    if number < threshold:
        return "oppose"
    return "neutral"


def _stance(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in {"support", "oppose"} else None


def _required_text(
    row: Mapping[str, Any],
    field: str,
    *,
    side: str,
    row_number: int,
) -> str:
    value = str(row.get(field) or "").strip()
    if not value:
        raise CalibrationDataError(
            f"missing_{side}_{field}: row={row_number}"
        )
    return value


def _decision_date(
    row: Mapping[str, Any],
    *,
    side: str,
    row_number: int,
) -> date:
    value = _required_text(
        row,
        "decision_date",
        side=side,
        row_number=row_number,
    )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CalibrationDataError(
            f"invalid_{side}_decision_date: row={row_number}"
        ) from exc
    if parsed.isoformat() != value:
        raise CalibrationDataError(
            f"invalid_{side}_decision_date: row={row_number}"
        )
    return parsed


def _timestamp(
    row: Mapping[str, Any],
    field: str,
    *,
    side: str,
    row_number: int,
) -> datetime:
    value = _required_text(row, field, side=side, row_number=row_number)
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CalibrationDataError(
            f"invalid_{field}: side={side} row={row_number}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CalibrationDataError(
            f"timezone_required: field={field} side={side} row={row_number}"
        )
    return parsed


def _validated_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    side: str,
    timestamp_field: str,
) -> tuple[
    dict[tuple[str, str, str], Mapping[str, Any]],
    dict[tuple[str, str, str], tuple[date, datetime]],
    dict[str, str] | None,
]:
    index: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    temporal: dict[tuple[str, str, str], tuple[date, datetime]] = {}
    lineage: dict[str, str] | None = None
    for row_number, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise CalibrationDataError(
                f"invalid_{side}_row: row={row_number}"
            )
        task_id = _required_text(
            row,
            "task_id",
            side=side,
            row_number=row_number,
        )
        code = _required_text(
            row,
            "code",
            side=side,
            row_number=row_number,
        )
        decision = _decision_date(
            row,
            side=side,
            row_number=row_number,
        )
        key = (task_id, code, decision.isoformat())
        if key in index:
            raise CalibrationDataError(
                f"duplicate_{side}_key: key={key}"
            )

        row_lineage = {
            field: _required_text(
                row,
                field,
                side=side,
                row_number=row_number,
            )
            for field in LINEAGE_FIELDS
        }
        split = _required_text(
            row,
            "evaluation_split",
            side=side,
            row_number=row_number,
        ).lower()
        if split != "oos":
            raise CalibrationDataError(
                f"{side}_not_oos: row={row_number}"
            )
        row_lineage["evaluation_split"] = split
        if lineage is None:
            lineage = row_lineage
        else:
            for field in LINEAGE_FIELDS:
                if lineage[field] != row_lineage[field]:
                    raise CalibrationDataError(
                        f"{field.removesuffix('_id')}_lineage_mismatch: "
                        f"side={side} row={row_number}"
                    )

        occurred_at = _timestamp(
            row,
            timestamp_field,
            side=side,
            row_number=row_number,
        )
        if side == "prediction":
            if occurred_at.date() < decision:
                raise CalibrationDataError(
                    f"prediction_before_decision_date: row={row_number}"
                )
            if occurred_at.date() > decision:
                raise CalibrationDataError(
                    f"prediction_after_decision_date: row={row_number}"
                )
        elif occurred_at.date() < decision:
            raise CalibrationDataError(
                f"settlement_before_decision_date: row={row_number}"
            )

        index[key] = row
        temporal[key] = (decision, occurred_at)
    return index, temporal, lineage


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def compute_calibration(
    stances: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
    *,
    outcome_key: str = "t3_close_ret",
    positive_threshold: float = 0.0,
) -> dict[str, Any]:
    """Measure immutable OOS predictions against explicit final outcomes.

    Rows join only on ``(task_id, code, decision_date)``. Duplicate identities,
    missing OOS lineage, mixed dataset/batch lineage, or invalid temporal order
    fail closed. Provisional, unresolved, censored, and unmatched rows stay
    visible in accounting and are excluded from confusion-matrix metrics.
    """
    prediction_index, prediction_temporal, prediction_lineage = _validated_rows(
        stances,
        side="prediction",
        timestamp_field="predicted_at",
    )
    outcome_index, outcome_temporal, outcome_lineage = _validated_rows(
        outcomes,
        side="outcome",
        timestamp_field="settled_at",
    )
    if prediction_lineage and outcome_lineage:
        for field in LINEAGE_FIELDS:
            if prediction_lineage[field] != outcome_lineage[field]:
                raise CalibrationDataError(
                    f"{field.removesuffix('_id')}_lineage_mismatch"
                )
    lineage = prediction_lineage or outcome_lineage

    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {
        "predictions": 0,
        "settled": 0,
        "correct": 0,
        "true_positive": 0,
        "true_negative": 0,
        "false_positive": 0, "false_negative": 0,
        "actual_positive": 0,
        "actual_negative": 0,
        "predicted_positive": 0,
        "predicted_negative": 0,
        "abstained": 0,
        "unmatched": 0,
        "non_final": 0,
        "invalid_outcome": 0,
    })
    for key, row in prediction_index.items():
        role = str(row.get("role") or "unknown")
        bucket = buckets[role]
        stance = _stance(row.get("stance"))
        if stance is None:
            bucket["abstained"] += 1
            continue
        bucket["predictions"] += 1
        outcome = outcome_index.get(key)
        if not outcome:
            bucket["unmatched"] += 1
            continue
        if (
            outcome.get("settlement_status") != "final"
            or outcome.get("resolved") is not True
        ):
            bucket["non_final"] += 1
            continue

        predicted_at = prediction_temporal[key][1]
        settled_at = outcome_temporal[key][1]
        if predicted_at >= settled_at:
            raise CalibrationDataError(
                f"prediction_not_before_settlement: key={key}"
            )

        actual = _outcome_label(outcome.get(outcome_key), positive_threshold)
        if actual not in {"support", "oppose"}:
            bucket["invalid_outcome"] += 1
            continue
        bucket["settled"] += 1
        if stance == "support":
            bucket["predicted_positive"] += 1
        else:
            bucket["predicted_negative"] += 1
        if actual == "support":
            bucket["actual_positive"] += 1
        else:
            bucket["actual_negative"] += 1

        if stance == "support" and actual == "support":
            bucket["true_positive"] += 1
            bucket["correct"] += 1
        elif stance == "oppose" and actual == "oppose":
            bucket["true_negative"] += 1
            bucket["correct"] += 1
        elif stance == "support" and actual == "oppose":
            bucket["false_positive"] += 1
        else:
            bucket["false_negative"] += 1

    metrics: dict[str, Any] = {}
    for role, bucket in sorted(buckets.items()):
        settled = bucket["settled"]
        actual_negative = (
            bucket["false_positive"] + bucket["true_negative"]
        )
        actual_positive = (
            bucket["false_negative"] + bucket["true_positive"]
        )
        predicted_positive = (
            bucket["true_positive"] + bucket["false_positive"]
        )
        metrics[role] = {
            **bucket,
            "accuracy": _rate(bucket["correct"], settled),
            "false_positive_rate": _rate(
                bucket["false_positive"],
                actual_negative,
            ),
            "false_negative_rate": _rate(
                bucket["false_negative"],
                actual_positive,
            ),
            "precision": _rate(
                bucket["true_positive"],
                predicted_positive,
            ),
            "recall": _rate(
                bucket["true_positive"],
                actual_positive,
            ),
            "status": "ok" if settled else "insufficient_data",
        }
    return {
        "schema": SCHEMA,
        "research_only": True,
        "automatic_strategy_mutation": False,
        "outcome_key": outcome_key,
        "positive_threshold": positive_threshold,
        "stance_rows": len(stances),
        "outcome_rows": len(outcomes),
        "join_fields": list(JOIN_FIELDS),
        "lineage": lineage,
        "class_definitions": {
            "positive": f"{outcome_key} > {positive_threshold}",
            "negative": f"{outcome_key} < {positive_threshold}",
            "neutral": (
                f"{outcome_key} == {positive_threshold}; excluded from metrics"
            ),
            "settled": (
                "settlement_status == 'final' and resolved is true and "
                "outcome is finite and directional"
            ),
        },
        "metric_definitions": dict(METRIC_DEFINITIONS),
        "roles": metrics,
        "status": "ok" if any(item["settled"] for item in metrics.values()) else "insufficient_data",
    }


def build_review_registry(
    report: Mapping[str, Any],
    *,
    min_accuracy: float = 0.5,
    min_settled: int = 20,
    asof: str | None = None,
) -> dict[str, Any]:
    """Create a human-review queue; never edits strategy configuration."""
    review = []
    for role, metric in (report.get("roles") or {}).items():
        if not isinstance(metric, Mapping):
            continue
        settled = int(metric.get("settled") or 0)
        accuracy = metric.get("accuracy")
        if settled >= min_settled and accuracy is not None and float(accuracy) < min_accuracy:
            review.append({
                "role": role,
                "reason": "accuracy_below_review_threshold",
                "accuracy": accuracy,
                "settled": settled,
            })
    return {
        "schema": REGISTRY_SCHEMA,
        "asof": str(asof or date.today().isoformat())[:10],
        "research_only": True,
        "automatic_strategy_mutation": False,
        "strategy_mutations": [],
        "thresholds": {"min_accuracy": min_accuracy, "min_settled": min_settled},
        "review_queue": review,
    }


def registry_file() -> str:
    return data_file("research-committee", "expert_calibration_registry.json")


def persist_review_registry(registry: Mapping[str, Any]) -> str:
    path = registry_file()
    atomic_write_json(path, dict(registry))
    return path
