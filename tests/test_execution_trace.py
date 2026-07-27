"""Execution trace contract tests (T01).

The trace exists to reconstruct one run end to end. These tests lock the three
properties that make it trustworthy: it never carries sensitive payloads, it
never invents a delivery receipt, and a corrupt or truncated file degrades to a
warning instead of taking a job down with it.
"""

import json
import os

import pytest

import execution_trace


@pytest.fixture(autouse=True)
def _trace_file(tmp_path, monkeypatch):
    path = tmp_path / "execution_trace.jsonl"
    monkeypatch.setenv(execution_trace.PATH_ENV, str(path))
    monkeypatch.delenv(execution_trace.SWITCH_ENV, raising=False)
    monkeypatch.delenv(execution_trace.TRACE_ID_ENV, raising=False)
    execution_trace.reset_degradations()
    yield path
    execution_trace.reset_degradations()


def _ctx(**overrides):
    base = {
        "trace_id": "trace-fixture",
        "batch_id": "a-share-20260727",
        "run_id": "job-a-20260727-1",
        "job_id": "job-a",
        "trading_date": "2026-07-27",
        "runtime": "local",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# field contract
# --------------------------------------------------------------------------


def test_emit_writes_allowlisted_fields_only(_trace_file):
    execution_trace.emit("job.started", **_ctx())
    events = execution_trace.read_events()

    assert len(events) == 1
    assert execution_trace.scan_sensitive_fields(events[0]) == []
    assert events[0]["schema"] == execution_trace.SCHEMA
    assert events[0]["trace_id"] == "trace-fixture"


@pytest.mark.parametrize(
    "field",
    ["prompt", "stdout", "stderr", "api_key", "response_body", "holdings"],
)
def test_sensitive_fields_cannot_enter_the_trace(field, _trace_file):
    with pytest.raises(ValueError):
        execution_trace.build_event("job.started", **_ctx(), **{field: "secret"})

    assert execution_trace.emit("job.started", **_ctx(), **{field: "secret"}) is None
    assert execution_trace.read_events() == []
    assert execution_trace.degradations()[0]["reason"] == "invalid_event"


def test_delivery_received_is_not_a_representable_event(_trace_file):
    with pytest.raises(ValueError):
        execution_trace.build_event("delivery.received", **_ctx())

    assert "delivery.received" not in execution_trace.EVENT_TYPES


def test_provider_acceptance_is_not_promoted_to_a_receipt(_trace_file):
    execution_trace.delivery_attempted(channel="feishu_direct", **_ctx())
    execution_trace.delivery_result("sent", channel="feishu_direct", **_ctx())

    types = [event["event_type"] for event in execution_trace.read_events()]
    assert types == ["delivery.attempted", "delivery.provider_accepted"]


def test_failed_delivery_records_a_reason_code(_trace_file):
    execution_trace.delivery_result("not_configured", channel="feishu_direct", **_ctx())

    event = execution_trace.read_events()[0]
    assert event["event_type"] == "delivery.failed"
    assert event["reason_codes"] == ["delivery_not_configured"]


def test_reason_codes_must_be_short_slugs(_trace_file):
    with pytest.raises(ValueError):
        execution_trace.build_event(
            "gate.blocked", **_ctx(), reason_codes=["stack trace: line 1\nline 2"]
        )


def test_long_values_are_bounded(_trace_file):
    execution_trace.emit("job.finished", **_ctx(status="x" * 5000))

    event = execution_trace.read_events()[0]
    assert len(event["status"]) <= 64


# --------------------------------------------------------------------------
# switch and degradation
# --------------------------------------------------------------------------


def test_switch_off_writes_nothing(_trace_file, monkeypatch):
    monkeypatch.setenv(execution_trace.SWITCH_ENV, "off")

    assert execution_trace.emit("job.started", **_ctx()) is None
    assert not os.path.exists(_trace_file)


def test_write_failure_degrades_without_raising(monkeypatch, tmp_path):
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("regular file", encoding="utf-8")
    monkeypatch.setenv(execution_trace.PATH_ENV, str(blocked / "trace.jsonl"))
    execution_trace.reset_degradations()

    assert execution_trace.emit("job.started", **_ctx()) is None
    assert execution_trace.degradations()[0]["reason"] == "write_failed"


# --------------------------------------------------------------------------
# reader resilience
# --------------------------------------------------------------------------


def test_reader_tolerates_truncation_bad_lines_and_duplicates(_trace_file):
    execution_trace.emit("job.started", **_ctx())
    good = _trace_file.read_text(encoding="utf-8").strip()
    with open(_trace_file, "a", encoding="utf-8") as handle:
        handle.write(good + "\n")                    # exact duplicate
        handle.write("{not json\n")                  # corrupt line
        handle.write('{"schema":"other_v1"}\n')      # foreign schema
        handle.write('{"schema":"a_stock_execution_event_v1","event_type":"nope"}\n')
        handle.write('{"schema":"a_stock_execution_event_v1","ev')  # truncated tail

    events, stats = execution_trace.read_events_with_stats()

    assert len(events) == 1
    assert stats["duplicate_events"] == 1
    assert stats["corrupt_lines"] == 4  # bad json, foreign schema, bad type, truncated tail


def test_reader_returns_empty_for_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv(execution_trace.PATH_ENV, str(tmp_path / "absent.jsonl"))

    assert execution_trace.read_events() == []


# --------------------------------------------------------------------------
# reconstruction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "terminal_status",
    ["ok", "blocked", "skipped_non_trading_day", "failed"],
)
def test_every_terminal_state_is_reconstructable_from_events(terminal_status, _trace_file):
    execution_trace.emit("job.started", **_ctx())
    execution_trace.emit("job.finished", **_ctx(status=terminal_status))

    runs = execution_trace.reconstruct_runs(execution_trace.read_events())

    assert runs["job-a-20260727-1"]["status"] == terminal_status
    assert runs["job-a-20260727-1"]["started_at"] is not None
    assert execution_trace.find_gaps(execution_trace.read_events()) == []


def test_gaps_flag_missing_start_missing_end_and_duplicate_terminal(_trace_file):
    execution_trace.emit("job.finished", **_ctx(run_id="orphan", status="ok"))
    execution_trace.emit("job.started", **_ctx(run_id="never-ends"))
    execution_trace.emit("job.started", **_ctx(run_id="twice"))
    execution_trace.emit("job.finished", **_ctx(run_id="twice", status="ok"))
    execution_trace.emit("job.finished", **_ctx(run_id="twice", status="failed"))

    gaps = {(gap["run_id"], gap["gap"]) for gap in execution_trace.find_gaps(execution_trace.read_events())}

    assert ("orphan", "missing_start") in gaps
    assert ("never-ends", "missing_terminal") in gaps
    assert ("twice", "duplicate_terminal") in gaps


def test_one_dag_shares_a_trace_id_across_distinct_runs(_trace_file):
    for job_id in ("dependency", "target"):
        run_id = f"{job_id}-run"
        execution_trace.emit("job.started", **_ctx(job_id=job_id, run_id=run_id))
        execution_trace.emit("job.finished", **_ctx(job_id=job_id, run_id=run_id, status="ok"))

    events = execution_trace.read_events()
    runs = execution_trace.reconstruct_runs(events)

    assert {event["trace_id"] for event in events} == {"trace-fixture"}
    assert set(runs) == {"dependency-run", "target-run"}


def test_trace_id_is_inherited_from_the_environment(monkeypatch):
    monkeypatch.setenv(execution_trace.TRACE_ID_ENV, "trace-from-dispatcher")

    assert execution_trace.resolve_trace_id() == "trace-from-dispatcher"


def test_trace_id_is_minted_when_absent(monkeypatch):
    monkeypatch.delenv(execution_trace.TRACE_ID_ENV, raising=False)

    minted = execution_trace.resolve_trace_id()

    assert minted and minted.startswith("trace-")
    assert execution_trace.resolve_trace_id(create=False) is None


def test_events_are_json_serialisable_one_per_line(_trace_file):
    execution_trace.emit("gate.passed", gate="dependency", **_ctx())
    execution_trace.emit("gate.blocked", gate="dependency", **_ctx(), reason_codes=["dep.x.missing"])

    lines = _trace_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["gate"] for line in lines] == ["dependency", "dependency"]
