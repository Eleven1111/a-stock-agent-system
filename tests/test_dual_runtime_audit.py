import json

from scripts import dual_runtime_audit as audit


def _write_state_identity(state_root):
    (state_root / "state_identity.json").write_text(
        json.dumps(
            {
                "state_id": "state-test",
                "created_at": "2026-07-02T00:00:00+00:00",
                "initial_root": str(state_root),
            }
        ),
        encoding="utf-8",
    )


def test_runtime_distribution_counts_each_runtime():
    runs = [{"runtime": "hermes"}, {"runtime": "openclaw"}, {"runtime": "hermes"}, {}]

    assert audit.runtime_distribution(runs) == {"hermes": 2, "openclaw": 1, "unknown": 1}


def test_detects_same_job_completed_by_two_runtimes_within_window():
    runs = [
        {
            "job_id": "capital-flow",
            "trading_date": "2026-07-02",
            "batch_id": "a-share-20260702",
            "runtime": "hermes",
            "status": "ok",
            "finished_at": "2026-07-02T10:30:00+00:00",
        },
        {
            "job_id": "capital-flow",
            "trading_date": "2026-07-02",
            "batch_id": "a-share-20260702",
            "runtime": "openclaw",
            "status": "ok",
            "finished_at": "2026-07-02T10:31:30+00:00",
        },
    ]

    findings = audit.detect_concurrent_duplicate_runs(runs, window_seconds=300)

    assert len(findings) == 1
    assert findings[0]["job_id"] == "capital-flow"
    assert findings[0]["runtimes"] == ["hermes", "openclaw"]
    assert findings[0]["spread_seconds"] == 90.0


def test_same_runtime_rerunning_is_not_flagged_as_duplicate():
    runs = [
        {
            "job_id": "capital-flow",
            "trading_date": "2026-07-02",
            "batch_id": "a-share-20260702",
            "runtime": "hermes",
            "status": "ok",
            "finished_at": "2026-07-02T10:30:00+00:00",
        },
        {
            "job_id": "capital-flow",
            "trading_date": "2026-07-02",
            "batch_id": "a-share-20260702",
            "runtime": "hermes",
            "status": "ok",
            "finished_at": "2026-07-02T14:30:00+00:00",
        },
    ]

    assert audit.detect_concurrent_duplicate_runs(runs, window_seconds=300) == []


def test_duplicate_runs_far_apart_are_not_flagged():
    runs = [
        {
            "job_id": "capital-flow",
            "trading_date": "2026-07-02",
            "batch_id": "a-share-20260702",
            "runtime": "hermes",
            "status": "ok",
            "finished_at": "2026-07-02T10:30:00+00:00",
        },
        {
            "job_id": "capital-flow",
            "trading_date": "2026-07-02",
            "batch_id": "a-share-20260702",
            "runtime": "openclaw",
            "status": "ok",
            "finished_at": "2026-07-02T14:30:00+00:00",
        },
    ]

    assert audit.detect_concurrent_duplicate_runs(runs, window_seconds=300) == []


def test_detects_overlapping_run_intervals_even_when_completions_are_far_apart():
    runs = [
        {
            "job_id": "capital-flow",
            "trading_date": "2026-07-02",
            "batch_id": "a-share-20260702",
            "runtime": "hermes",
            "status": "ok",
            "started_at": "2026-07-02T10:00:00+00:00",
            "finished_at": "2026-07-02T11:00:00+00:00",
        },
        {
            "job_id": "capital-flow",
            "trading_date": "2026-07-02",
            "batch_id": "a-share-20260702",
            "runtime": "openclaw",
            "status": "ok",
            "started_at": "2026-07-02T10:30:00+00:00",
            "finished_at": "2026-07-02T12:00:00+00:00",
        },
    ]

    findings = audit.detect_concurrent_duplicate_runs(runs, window_seconds=300)

    assert len(findings) == 1
    assert findings[0]["detection_basis"] == "interval_overlap"
    assert findings[0]["overlap_seconds"] == 1800.0


def test_complete_non_overlapping_intervals_override_close_completion_heuristic():
    runs = [
        {
            "job_id": "capital-flow",
            "trading_date": "2026-07-02",
            "batch_id": "a-share-20260702",
            "runtime": "hermes",
            "status": "ok",
            "started_at": "2026-07-02T10:00:00+00:00",
            "finished_at": "2026-07-02T10:04:00+00:00",
        },
        {
            "job_id": "capital-flow",
            "trading_date": "2026-07-02",
            "batch_id": "a-share-20260702",
            "runtime": "openclaw",
            "status": "ok",
            "started_at": "2026-07-02T10:05:00+00:00",
            "finished_at": "2026-07-02T10:06:00+00:00",
        },
    ]

    assert audit.detect_concurrent_duplicate_runs(runs, window_seconds=300) == []


def test_active_leases_reads_held_lock_directories(tmp_path):
    lease_dir = tmp_path / "runtime" / "leases" / "2026-07-02" / "a-share-20260702" / "capital-flow.lease"
    lease_dir.mkdir(parents=True)
    (lease_dir / "holder.json").write_text(
        json.dumps({"runtime": "hermes", "acquired_at": "2026-07-02T10:00:00+00:00"}),
        encoding="utf-8",
    )

    held = audit.active_leases(str(tmp_path))

    assert len(held) == 1
    assert held[0]["job_id"] == "capital-flow"
    assert held[0]["trading_date"] == "2026-07-02"
    assert held[0]["runtime"] == "hermes"


def test_active_leases_empty_when_no_leases_dir(tmp_path):
    assert audit.active_leases(str(tmp_path)) == []


def test_openclaw_registration_check_reports_unavailable_when_binary_missing(monkeypatch):
    monkeypatch.setattr(audit.shutil, "which", lambda name: None)

    result = audit.openclaw_registration_check({"jobs": []})

    assert result["status"] == "unavailable"


def test_openclaw_registration_check_flags_missing_and_orphaned_jobs(monkeypatch):
    monkeypatch.setattr(audit.shutil, "which", lambda name: "/usr/local/bin/openclaw")

    class _Completed:
        returncode = 0
        stdout = json.dumps(
            {
                "jobs": [
                    {"id": "opaque-123", "name": "A-stock: capital-flow"},
                    {"id": "opaque-456", "name": "A-stock: ghost-job"},
                    {"id": "unrelated-789", "name": "Personal reminder"},
                ]
            }
        )
        stderr = ""

    monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: _Completed())

    manifest = {
        "jobs": [
            {"id": "capital-flow", "enabled": True, "deliver": "feishu_direct"},
            {"id": "event-calendar", "enabled": True, "deliver": "feishu_direct"},
        ]
    }

    result = audit.openclaw_registration_check(manifest)

    assert result["status"] == "ok"
    assert result["installed_count"] == 2
    assert result["missing_from_openclaw"] == ["event-calendar"]
    assert result["orphaned_in_openclaw"] == ["ghost-job"]


def test_openclaw_registration_maps_opaque_instance_id_by_managed_name(monkeypatch):
    monkeypatch.setattr(audit.shutil, "which", lambda name: "/usr/local/bin/openclaw")

    class _Completed:
        returncode = 0
        stdout = json.dumps(
            {"jobs": [{"id": "7f38f5d4", "name": "A-stock: capital-flow"}]}
        )
        stderr = ""

    monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: _Completed())
    manifest = {
        "jobs": [
            {"id": "capital-flow", "enabled": True, "deliver": "feishu_direct"}
        ]
    }

    result = audit.openclaw_registration_check(manifest)

    assert result["missing_from_openclaw"] == []
    assert result["orphaned_in_openclaw"] == []
    assert result["installed_count"] == 1


def test_build_report_is_clean_when_no_findings(tmp_path):
    _write_state_identity(tmp_path)
    manifest = {"jobs": []}
    report = audit.build_report(manifest, [], state_root=str(tmp_path), check_openclaw=False)

    assert report["clean"] is True
    assert report["status"] == "ok"
    assert report["concurrent_duplicate_runs"] == []
    assert report["active_leases"] == []
    assert report["openclaw_registration"] == {"status": "skipped"}


def test_build_report_is_blocked_when_registration_inventory_is_unavailable(
    tmp_path,
    monkeypatch,
):
    _write_state_identity(tmp_path)
    monkeypatch.setattr(
        audit,
        "openclaw_registration_check",
        lambda manifest: {"status": "unavailable", "reason": "binary missing"},
    )

    report = audit.build_report({"jobs": []}, [], state_root=str(tmp_path))

    assert report["status"] == "blocked"
    assert report["clean"] is False


def test_build_report_is_blocked_when_state_identity_query_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        audit,
        "state_identity_summary",
        lambda state_root: {"status": "error", "state_root": state_root},
    )

    report = audit.build_report(
        {"jobs": []},
        [],
        state_root=str(tmp_path),
        check_openclaw=False,
    )

    assert report["status"] == "blocked"
    assert report["clean"] is False


def test_build_report_is_blocked_when_run_inventory_is_invalid(tmp_path):
    _write_state_identity(tmp_path)

    report = audit.build_report(
        {"jobs": []},
        None,
        state_root=str(tmp_path),
        check_openclaw=False,
    )

    assert report["runtime_inventory"]["status"] == "error"
    assert report["status"] == "blocked"
    assert report["clean"] is False


def test_build_report_is_blocked_when_lease_inventory_query_fails(
    tmp_path,
    monkeypatch,
):
    _write_state_identity(tmp_path)
    monkeypatch.setattr(
        audit,
        "_active_leases_inventory",
        lambda state_root: ([], {"status": "error", "reason": "read failed"}),
    )

    report = audit.build_report(
        {"jobs": []},
        [],
        state_root=str(tmp_path),
        check_openclaw=False,
    )

    assert report["lease_inventory"]["status"] == "error"
    assert report["status"] == "blocked"
    assert report["clean"] is False


def test_registration_mismatch_prevents_clean_report(tmp_path, monkeypatch):
    _write_state_identity(tmp_path)
    monkeypatch.setattr(
        audit,
        "openclaw_registration_check",
        lambda manifest: {
            "status": "ok",
            "missing_from_openclaw": ["capital-flow"],
            "orphaned_in_openclaw": [],
        },
    )

    report = audit.build_report({"jobs": []}, [], state_root=str(tmp_path))

    assert report["status"] == "blocked"
    assert report["clean"] is False


def test_main_prints_json_report(tmp_path, monkeypatch, capsys):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
    state_home = tmp_path / "state"
    state_home.mkdir()
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state_home))

    argv = [
        "--manifest",
        str(manifest_path),
        "--no-openclaw-check",
    ]
    monkeypatch.setattr("sys.argv", ["dual_runtime_audit.py", *argv])
    exit_code = audit.main()
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["schema"] == "a_stock_dual_runtime_audit_v1"
    assert output["state_identity"]["state_root"] == str(state_home)
