import json

import pytest

import learning_ledger


def _proposal(ledger_file):
    return learning_ledger.propose_case(
        source={
            "kind": "research_consumer_run",
            "artifact_ref": "/tmp/run.json",
            "artifact_sha256": "a" * 64,
        },
        observation={
            "classification": "runtime_failure",
            "status": "failed",
            "reason_codes": ["runtime_exception"],
        },
        reproduction={
            "runtime": "hermes",
            "role": "risk_redteam",
            "evidence_pack_ref": "sha256:pack",
            "frozen_at": "2026-08-10T10:00:00+08:00",
        },
        now="2026-08-10T10:05:00+08:00",
        ledger_file=str(ledger_file),
    )


def _benchmark():
    return {
        "frozen_now": "2026-08-10T10:00:00+08:00",
        "category": "runtime_failure",
        "role": "risk_redteam",
        "evidence_pack": {
            "schema": "research_evidence_pack_v1",
            "ref": "fixture-runtime-failure",
            "builder_version": "evidence_pack_v1",
            "payload": {
                "task_id": "eval-task",
                "quality": {"status": "ok", "missing": [], "degraded": []},
            },
        },
        "payload": {"__raise__": "exception"},
        "expected_status": "failed",
        "expected_reason_codes": ["runtime_exception"],
        "note": "A runtime crash remains a terminal failure.",
    }


def test_proposal_is_append_only_and_idempotent_by_fingerprint(tmp_path):
    ledger = tmp_path / "learning.jsonl"

    first = _proposal(ledger)
    second = _proposal(ledger)

    assert first["created"] is True
    assert second["created"] is False
    assert first["case"]["case_id"] == second["case"]["case_id"]
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1
    projected = learning_ledger.project_cases(str(ledger))
    assert projected[0]["status"] == "candidate"
    assert projected[0]["automatic_effect"] == "none"


def test_accepting_case_requires_reproducible_benchmark(tmp_path):
    ledger = tmp_path / "learning.jsonl"
    case_id = _proposal(ledger)["case"]["case_id"]

    with pytest.raises(learning_ledger.LearningCaseError, match="benchmark_required"):
        learning_ledger.review_case(
            case_id,
            decision="accepted",
            reviewer="human-reviewer",
            benchmark=None,
            now="2026-08-10T11:00:00+08:00",
            ledger_file=str(ledger),
        )


def test_review_appends_event_without_rewriting_proposal(tmp_path):
    ledger = tmp_path / "learning.jsonl"
    case_id = _proposal(ledger)["case"]["case_id"]

    reviewed = learning_ledger.review_case(
        case_id,
        decision="accepted",
        reviewer="human-reviewer",
        benchmark=_benchmark(),
        now="2026-08-10T11:00:00+08:00",
        ledger_file=str(ledger),
    )

    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [line["event_type"] for line in lines] == [
        "learning.case.proposed",
        "learning.case.reviewed",
    ]
    assert reviewed["case"]["status"] == "accepted"
    assert reviewed["case"]["benchmark"]["expected_status"] == "failed"
    assert reviewed["case"]["automatic_effect"] == "benchmark_only"


def test_review_rejects_unknown_case_and_unbounded_benchmark_fields(tmp_path):
    ledger = tmp_path / "learning.jsonl"

    with pytest.raises(learning_ledger.LearningCaseError, match="case_not_found"):
        learning_ledger.review_case(
            "lc-missing",
            decision="rejected",
            reviewer="human-reviewer",
            now="2026-08-10T11:00:00+08:00",
            ledger_file=str(ledger),
        )

    case_id = _proposal(ledger)["case"]["case_id"]
    benchmark = {**_benchmark(), "arbitrary_python": "import os"}
    with pytest.raises(learning_ledger.LearningCaseError, match="benchmark_field_not_allowed"):
        learning_ledger.review_case(
            case_id,
            decision="accepted",
            reviewer="human-reviewer",
            benchmark=benchmark,
            now="2026-08-10T11:00:00+08:00",
            ledger_file=str(ledger),
        )


def test_timezone_and_reason_code_contracts_fail_closed(tmp_path):
    ledger = tmp_path / "learning.jsonl"

    with pytest.raises(learning_ledger.LearningCaseError, match="timezone_required"):
        learning_ledger.propose_case(
            source={"kind": "x", "artifact_ref": "/tmp/x", "artifact_sha256": "a" * 64},
            observation={
                "classification": "runtime_failure",
                "status": "failed",
                "reason_codes": ["runtime_exception"],
            },
            reproduction={"frozen_at": "2026-08-10T10:00:00"},
            now="2026-08-10T10:05:00+08:00",
            ledger_file=str(ledger),
        )

    with pytest.raises(learning_ledger.LearningCaseError, match="reason_code_invalid"):
        learning_ledger.propose_case(
            source={"kind": "x", "artifact_ref": "/tmp/x", "artifact_sha256": "a" * 64},
            observation={
                "classification": "runtime_failure",
                "status": "failed",
                "reason_codes": ["runtime exception with spaces"],
            },
            reproduction={"frozen_at": "2026-08-10T10:00:00+08:00"},
            now="2026-08-10T10:05:00+08:00",
            ledger_file=str(ledger),
        )


def test_tampered_ledger_event_fails_closed(tmp_path):
    ledger = tmp_path / "learning.jsonl"
    _proposal(ledger)
    event = json.loads(ledger.read_text(encoding="utf-8"))
    event["payload"]["observation"]["status"] = "submitted"
    ledger.write_text(json.dumps(event) + "\n", encoding="utf-8")

    with pytest.raises(learning_ledger.LearningCaseError, match="ledger_hash_mismatch"):
        learning_ledger.project_cases(str(ledger))
