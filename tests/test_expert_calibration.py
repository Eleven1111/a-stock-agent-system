import json

import pytest

import expert_calibration as calibration
from scripts import expert_calibration as calibration_cli


LINEAGE = {
    "dataset_id": "committee-oos-2026h1-v1",
    "batch_id": "committee-oos-2026h1-b1",
    "evaluation_split": "oos",
}


def _stance(
    task_id,
    code,
    decision_date,
    *,
    role="risk_aggressive",
    stance="support",
    predicted_at=None,
    **overrides,
):
    return {
        "task_id": task_id,
        "code": code,
        "decision_date": decision_date,
        "role": role,
        "stance": stance,
        "predicted_at": predicted_at or f"{decision_date}T09:35:00+08:00",
        **LINEAGE,
        **overrides,
    }


def _outcome(
    task_id,
    code,
    decision_date,
    *,
    t3_close_ret,
    settled_at=None,
    **overrides,
):
    return {
        "task_id": task_id,
        "code": code,
        "decision_date": decision_date,
        "settlement_status": "final",
        "resolved": True,
        "settled_at": settled_at or "2026-07-06T15:05:00+08:00",
        "t3_close_ret": t3_close_ret,
        **LINEAGE,
        **overrides,
    }


def test_calibration_uses_exact_key_final_oos_lineage_and_confusion_denominators():
    report = calibration.compute_calibration(
        [
            _stance("t1", "600519", "2026-07-01"),
            _stance("t2", "000001", "2026-07-02"),
            _stance("t3", "000002", "2026-07-03", stance="oppose"),
            _stance(
                "t4",
                "000003",
                "2026-07-03",
                role="risk_neutral",
                stance="abstain",
            ),
        ],
        [
            _outcome("t1", "600519", "2026-07-01", t3_close_ret=3.0),
            _outcome("t2", "000001", "2026-07-02", t3_close_ret=-1.0),
            _outcome("t3", "000002", "2026-07-03", t3_close_ret=-2.0),
            _outcome("t4", "000003", "2026-07-03", t3_close_ret=1.0),
        ],
    )

    aggressive = report["roles"]["risk_aggressive"]
    assert report["research_only"] is True
    assert report["automatic_strategy_mutation"] is False
    assert report["lineage"] == LINEAGE
    assert aggressive["settled"] == 3
    assert aggressive["true_positive"] == 1
    assert aggressive["false_positive"] == 1
    assert aggressive["true_negative"] == 1
    assert aggressive["false_negative"] == 0
    assert aggressive["accuracy"] == 0.6667
    assert aggressive["false_positive_rate"] == 0.5
    assert aggressive["false_negative_rate"] == 0.0
    assert report["metric_definitions"]["false_positive_rate"] == (
        "false_positive / (false_positive + true_negative)"
    )
    assert report["roles"]["risk_neutral"]["abstained"] == 1


def test_calibration_has_no_code_only_or_partial_key_fallback():
    report = calibration.compute_calibration(
        [_stance("task-a", "600519", "2026-07-01")],
        [_outcome("task-b", "600519", "2026-07-01", t3_close_ret=3.0)],
    )

    metric = report["roles"]["risk_aggressive"]
    assert metric["settled"] == 0
    assert metric["unmatched"] == 1
    assert metric["status"] == "insufficient_data"


@pytest.mark.parametrize("duplicate_side", ["prediction", "outcome"])
def test_calibration_rejects_duplicate_composite_keys(duplicate_side):
    stance = _stance("t1", "600519", "2026-07-01")
    outcome = _outcome("t1", "600519", "2026-07-01", t3_close_ret=3.0)
    stances = [stance, dict(stance)] if duplicate_side == "prediction" else [stance]
    outcomes = [outcome, dict(outcome)] if duplicate_side == "outcome" else [outcome]

    with pytest.raises(
        calibration.CalibrationDataError,
        match=rf"duplicate_{duplicate_side}_key",
    ):
        calibration.compute_calibration(stances, outcomes)


@pytest.mark.parametrize(
    "outcome_overrides",
    [
        {"settlement_status": "provisional"},
        {"settlement_status": None},
        {"resolved": False},
        {"resolved": None},
    ],
)
def test_calibration_excludes_outcomes_without_explicit_final_resolution(
    outcome_overrides,
):
    report = calibration.compute_calibration(
        [_stance("t1", "600519", "2026-07-01")],
        [
            _outcome(
                "t1",
                "600519",
                "2026-07-01",
                t3_close_ret=3.0,
                **outcome_overrides,
            )
        ],
    )

    metric = report["roles"]["risk_aggressive"]
    assert metric["settled"] == 0
    assert metric["non_final"] == 1


@pytest.mark.parametrize(
    ("stance_overrides", "outcome_overrides", "error"),
    [
        ({"predicted_at": "2026-07-01T09:35:00"}, {}, "timezone_required"),
        ({"predicted_at": "2026-06-30T15:00:00+08:00"}, {}, "prediction_before_decision_date"),
        (
            {"predicted_at": "2026-07-01T15:05:00+08:00"},
            {"settled_at": "2026-07-01T15:05:00+08:00"},
            "prediction_not_before_settlement",
        ),
        ({}, {"settled_at": "not-a-time"}, "invalid_settled_at"),
    ],
)
def test_calibration_rejects_invalid_temporal_order(
    stance_overrides,
    outcome_overrides,
    error,
):
    with pytest.raises(calibration.CalibrationDataError, match=error):
        calibration.compute_calibration(
            [
                _stance(
                    "t1",
                    "600519",
                    "2026-07-01",
                    **stance_overrides,
                )
            ],
            [
                _outcome(
                    "t1",
                    "600519",
                    "2026-07-01",
                    t3_close_ret=3.0,
                    **outcome_overrides,
                )
            ],
        )


@pytest.mark.parametrize(
    ("side", "overrides", "error"),
    [
        ("prediction", {"dataset_id": ""}, "missing_prediction_dataset_id"),
        ("outcome", {"batch_id": ""}, "missing_outcome_batch_id"),
        ("prediction", {"evaluation_split": "is"}, "prediction_not_oos"),
        ("outcome", {"evaluation_split": "shadow"}, "outcome_not_oos"),
        ("outcome", {"dataset_id": "other-dataset"}, "dataset_lineage_mismatch"),
        ("outcome", {"batch_id": "other-batch"}, "batch_lineage_mismatch"),
    ],
)
def test_calibration_requires_matching_oos_dataset_and_batch_lineage(
    side,
    overrides,
    error,
):
    stance_overrides = overrides if side == "prediction" else {}
    outcome_overrides = overrides if side == "outcome" else {}

    with pytest.raises(calibration.CalibrationDataError, match=error):
        calibration.compute_calibration(
            [_stance("t1", "600519", "2026-07-01", **stance_overrides)],
            [
                _outcome(
                    "t1",
                    "600519",
                    "2026-07-01",
                    t3_close_ret=3.0,
                    **outcome_overrides,
                )
            ],
        )


def test_calibration_review_registry_is_research_only_and_never_mutates_strategy():
    report = {"roles": {"risk_redteam": {"settled": 20, "accuracy": 0.2}}}
    registry = calibration.build_review_registry(
        report,
        min_settled=20,
        min_accuracy=0.5,
    )

    assert registry["schema"] == "expert_calibration_registry_v1"
    assert registry["research_only"] is True
    assert registry["automatic_strategy_mutation"] is False
    assert registry["review_queue"][0]["role"] == "risk_redteam"


def test_cli_loader_does_not_silently_drop_invalid_rows(tmp_path):
    source = tmp_path / "stances.json"
    source.write_text(json.dumps(["not-a-row"]), encoding="utf-8")

    with pytest.raises(
        calibration.CalibrationDataError,
        match="invalid_prediction_row",
    ):
        calibration.compute_calibration(calibration_cli._rows(str(source)), [])
