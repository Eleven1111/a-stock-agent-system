"""Shadow-gate report tests (T02).

The report is what turns shadow observation into a go/no-go decision, so the
signals it computes have to be trustworthy: real gaps must surface, and a
provider acknowledgement must never be reported as a user receipt.
"""

import json

import pytest

import execution_trace
from scripts import execution_trace_report as report


@pytest.fixture(autouse=True)
def _trace_file(tmp_path, monkeypatch):
    path = tmp_path / "execution_trace.jsonl"
    monkeypatch.setenv(execution_trace.PATH_ENV, str(path))
    monkeypatch.delenv(execution_trace.SWITCH_ENV, raising=False)
    execution_trace.reset_degradations()
    return path


def _ctx(**overrides):
    base = {
        "trace_id": "trace-1",
        "batch_id": "a-share-20260612",
        "run_id": "job-a-run-1",
        "job_id": "job-a",
        "trading_date": "2026-06-12",
        "runtime": "local",
    }
    base.update(overrides)
    return base


def _complete_run(run_id, job_id, status="ok"):
    ctx = _ctx(run_id=run_id, job_id=job_id)
    execution_trace.emit("job.started", **ctx)
    execution_trace.emit("job.finished", **ctx, status=status)


def test_report_counts_terminal_states_per_job():
    _complete_run("run-1", "job-a")
    _complete_run("run-2", "job-b", status="blocked")

    built = report.build_report(execution_trace.read_events())

    assert built["run_count"] == 2
    assert built["completion_rate"] == 1.0
    assert built["status_counts"] == {"blocked": 1, "ok": 1}
    assert built["per_job_status"]["job-b"] == {"blocked": 1}


def test_report_surfaces_gaps_instead_of_hiding_them():
    execution_trace.emit("job.finished", **_ctx(run_id="orphan"), status="ok")

    built = report.build_report(execution_trace.read_events())

    assert built["trace_gaps"] == [
        {"run_id": "orphan", "job_id": "job-a", "gap": "missing_start"}
    ]
    assert built["shadow_gate"]["no_terminal_without_start"] is False


def test_duplicate_terminal_fails_the_shadow_gate():
    ctx = _ctx(run_id="twice")
    execution_trace.emit("job.started", **ctx)
    execution_trace.emit("job.finished", **ctx, status="ok")
    execution_trace.emit("job.finished", **ctx, status="failed")

    built = report.build_report(execution_trace.read_events())

    assert built["shadow_gate"]["no_duplicate_terminal"] is False


def test_delivery_acceptance_is_never_reported_as_a_receipt():
    ctx = _ctx(run_id="delivered")
    execution_trace.emit("job.started", **ctx)
    execution_trace.delivery_attempted(channel="feishu_direct", **ctx)
    execution_trace.delivery_result("sent", channel="feishu_direct", **ctx)
    execution_trace.emit("job.finished", **ctx, status="ok")

    delivery = report.build_report(execution_trace.read_events())["delivery"]

    assert delivery["attempted"] == 1
    assert delivery["provider_accepted"] == 1
    assert delivery["receipt_known"] is False
    assert "not a user receipt" in delivery["note"]


def test_failed_delivery_is_counted_separately():
    ctx = _ctx(run_id="undelivered")
    execution_trace.emit("job.started", **ctx)
    execution_trace.delivery_attempted(channel="feishu_direct", **ctx)
    execution_trace.delivery_result("failed", channel="feishu_direct", **ctx)
    execution_trace.emit("job.finished", **ctx, status="ok")

    delivery = report.build_report(execution_trace.read_events())["delivery"]

    assert delivery["failed"] == 1
    assert delivery["provider_acceptance_rate"] == 0.0


def test_blocked_reason_codes_are_aggregated():
    ctx = _ctx(run_id="blocked-run")
    execution_trace.emit("job.started", **ctx)
    execution_trace.emit("gate.blocked", gate="dependency",
                         reason_codes=["dep.upstream.missing"], **ctx)
    execution_trace.emit("job.finished", status="blocked",
                         reason_codes=["dep.upstream.missing"], **ctx)

    built = report.build_report(execution_trace.read_events())

    assert built["blocked_reason_codes"] == {"dep.upstream.missing": 1}


def test_coverage_lists_enabled_jobs_without_a_terminal_event(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [
        {"id": "job-a", "enabled": True},
        {"id": "job-b", "enabled": True},
        {"id": "job-c", "enabled": False},
    ]}), encoding="utf-8")
    _complete_run("run-1", "job-a")

    coverage = report.coverage_report(str(manifest), execution_trace.read_events())

    assert coverage["enabled_jobs"] == 2
    assert coverage["jobs_with_terminal_event"] == 1
    assert coverage["missing_jobs"] == ["job-b"]


def test_report_tolerates_a_corrupt_trace_file(_trace_file):
    _complete_run("run-1", "job-a")
    with open(_trace_file, "a", encoding="utf-8") as handle:
        handle.write("{truncated\n")

    events, stats = execution_trace.read_events_with_stats()
    built = report.build_report(events, stats=stats)

    assert built["read_stats"]["corrupt_lines"] == 1
    assert built["run_count"] == 1
