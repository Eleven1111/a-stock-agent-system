import json

import pytest

import learning_eval_factory as factory
import learning_ledger
from scripts import evaluate_agent_harness


def _write_run(path, *, status, reason_codes=None, evidence_pack_ref="sha256:pack"):
    path.write_text(
        json.dumps(
            {
                "schema": "research_consumer_run_v1",
                "research_only": True,
                "trading_action": "none",
                "runtime": "hermes",
                "worker": "hermes-primary",
                "started_at": "2026-08-10T10:00:00+08:00",
                "status": status,
                "task_id": "rt-1",
                "role": "risk_redteam",
                "evidence_pack_ref": evidence_pack_ref,
                "reason_codes": reason_codes or [],
            }
        ),
        encoding="utf-8",
    )


def _benchmark():
    return {
        "frozen_now": "2026-08-10T10:00:00+08:00",
        "category": "runtime_failure",
        "role": "risk_redteam",
        "evidence_pack": {
            "schema": "research_evidence_pack_v1",
            "ref": "source-ref-does-not-become-fixture-name",
            "builder_version": "evidence_pack_v1",
            "payload": {
                "task_id": "eval-task",
                "quality": {"status": "ok", "missing": [], "degraded": []},
            },
        },
        "payload": {"__raise__": "exception"},
        "expected_status": "failed",
        "expected_reason_codes": ["runtime_exception"],
        "note": "Frozen regression for a runtime crash.",
    }


def test_scan_discovers_failures_but_skips_healthy_and_corrupt_runs(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_run(runs / "failed.json", status="failed", reason_codes=["runtime_exception"])
    _write_run(runs / "submitted.json", status="submitted")
    (runs / "corrupt.json").write_text("{", encoding="utf-8")
    ledger = tmp_path / "learning.jsonl"

    first = factory.discover_consumer_failures(
        str(runs), ledger_file=str(ledger), now="2026-08-10T12:00:00+08:00"
    )
    second = factory.discover_consumer_failures(
        str(runs), ledger_file=str(ledger), now="2026-08-10T12:01:00+08:00"
    )

    assert first == {"scanned": 3, "eligible": 1, "created": 1, "duplicates": 0, "invalid": 1}
    assert second["created"] == 0
    assert second["duplicates"] == 1
    case = learning_ledger.project_cases(str(ledger))[0]
    assert case["observation"]["classification"] == "runtime_failure"
    assert case["reproduction"]["evidence_pack_ref"] == "sha256:pack"


def test_scan_classifies_configuration_and_evidence_gaps(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_run(
        runs / "config.json",
        status="blocked",
        reason_codes=["runtime_turn_unconfigured"],
        evidence_pack_ref="",
    )
    _write_run(
        runs / "evidence.json",
        status="abstained",
        reason_codes=["evidence_pack_insufficient"],
    )
    ledger = tmp_path / "learning.jsonl"

    report = factory.discover_consumer_failures(
        str(runs), ledger_file=str(ledger), now="2026-08-10T12:00:00+08:00"
    )

    assert report["created"] == 2
    classifications = {
        case["observation"]["classification"]
        for case in learning_ledger.project_cases(str(ledger))
    }
    assert classifications == {"configuration_gap", "evidence_gap"}


def test_materialize_suite_exports_only_accepted_cases_and_replays(tmp_path):
    ledger = tmp_path / "learning.jsonl"
    proposal = learning_ledger.propose_case(
        source={"kind": "test", "artifact_ref": "/tmp/a", "artifact_sha256": "b" * 64},
        observation={
            "classification": "runtime_failure",
            "status": "failed",
            "reason_codes": ["runtime_exception"],
        },
        reproduction={"runtime": "hermes", "role": "risk_redteam", "frozen_at": "2026-08-10T10:00:00+08:00"},
        now="2026-08-10T12:00:00+08:00",
        ledger_file=str(ledger),
    )
    learning_ledger.review_case(
        proposal["case"]["case_id"],
        decision="accepted",
        reviewer="human-reviewer",
        benchmark=_benchmark(),
        now="2026-08-10T12:05:00+08:00",
        ledger_file=str(ledger),
    )
    rejected = learning_ledger.propose_case(
        source={"kind": "test", "artifact_ref": "/tmp/b", "artifact_sha256": "c" * 64},
        observation={"classification": "contract_failure", "status": "blocked", "reason_codes": ["bad_schema"]},
        reproduction={"runtime": "openclaw", "role": "evidence_auditor", "frozen_at": "2026-08-10T10:00:00+08:00"},
        now="2026-08-10T12:00:00+08:00",
        ledger_file=str(ledger),
    )
    learning_ledger.review_case(
        rejected["case"]["case_id"],
        decision="rejected",
        reviewer="human-reviewer",
        now="2026-08-10T12:05:00+08:00",
        ledger_file=str(ledger),
    )

    output = tmp_path / "suite"
    report = factory.materialize_eval_suite(str(ledger), str(output))

    cases = json.loads((output / "cases.json").read_text(encoding="utf-8"))
    assert report["cases"] == 1
    assert len(cases["cases"]) == 1
    fixture_name = cases["cases"][0]["evidence_pack"]
    assert fixture_name.startswith("learning-")
    assert (output / "fixtures" / f"{fixture_name}.json").exists()

    replay = evaluate_agent_harness.evaluate(
        cases_path=str(output / "cases.json"), runtimes=("hermes", "openclaw")
    )
    assert replay["metrics"]["all_hard_metrics_met"] is True
    assert replay["metrics"]["runs"] == 2


def test_materialize_refuses_empty_or_unreviewed_suite(tmp_path):
    ledger = tmp_path / "learning.jsonl"
    _write_run(tmp_path / "run.json", status="failed", reason_codes=["runtime_exception"])

    with pytest.raises(factory.EvalFactoryError, match="no_accepted_learning_cases"):
        factory.materialize_eval_suite(str(ledger), str(tmp_path / "suite"))
