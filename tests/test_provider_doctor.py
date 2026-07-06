"""Provider canary reports health per dataset instead of per library."""

from __future__ import annotations

import os
import time

from scripts import provider_doctor


def test_optional_dataset_failure_degrades_without_hiding_healthy_datasets(monkeypatch):
    monkeypatch.setattr(
        provider_doctor,
        "PROBES",
        {
            "tencent_quote": {
                "provider": "tencent",
                "required": True,
                "call": lambda: {"sh000001": {"price": 3000}},
            },
            "eastmoney_fund_flow": {
                "provider": "eastmoney",
                "required": False,
                "call": lambda: (_ for _ in ()).throw(ConnectionError("blocked")),
            },
            "akshare_limitup": {
                "provider": "akshare_push2ex",
                "required": False,
                "call": lambda: [{"code": "600001"}],
            },
        },
    )

    report = provider_doctor.run_probes()

    assert report["status"] == "degraded"
    assert report["datasets"]["tencent_quote"]["status"] == "ok"
    assert report["datasets"]["eastmoney_fund_flow"]["status"] == "error"
    assert report["datasets"]["akshare_limitup"]["status"] == "ok"


def test_required_dataset_failure_is_error(monkeypatch):
    monkeypatch.setattr(
        provider_doctor,
        "PROBES",
        {
            "tencent_quote": {
                "provider": "tencent",
                "required": True,
                "call": lambda: {},
            }
        },
    )

    assert provider_doctor.run_probes()["status"] == "error"


def _write_lock(job_dir, run_file, *, age_seconds, with_artifact=False):
    os.makedirs(job_dir, exist_ok=True)
    lock_path = os.path.join(job_dir, f"{run_file}.lock")
    with open(lock_path, "w", encoding="utf-8") as handle:
        handle.write("")
    stale = time.time() - age_seconds
    os.utime(lock_path, (stale, stale))
    if with_artifact:
        with open(os.path.join(job_dir, run_file), "w", encoding="utf-8") as handle:
            handle.write("{}")
    return lock_path


def test_artifact_integrity_flags_stale_orphan_lock(tmp_path):
    job_dir = tmp_path / "hot-money-context"
    _write_lock(str(job_dir), "run-abc.json", age_seconds=1200)

    result = provider_doctor._scan_orphan_locks(str(tmp_path))

    assert result["status"] == "error"
    assert result["orphan_lock_count"] == 1
    entry = result["orphan_locks"][0]
    assert entry["job"] == "hot-money-context"
    assert entry["run_file"] == "run-abc.json"


def test_artifact_integrity_ignores_fresh_or_resolved_locks(tmp_path):
    fresh_dir = tmp_path / "fresh-job"
    _write_lock(str(fresh_dir), "run-fresh.json", age_seconds=60)
    resolved_dir = tmp_path / "resolved-job"
    _write_lock(str(resolved_dir), "run-done.json", age_seconds=1200, with_artifact=True)

    result = provider_doctor._scan_orphan_locks(str(tmp_path))

    assert result["status"] == "ok"
    assert result["orphan_lock_count"] == 0


def test_run_probes_orphan_lock_drives_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        provider_doctor,
        "PROBES",
        {
            "tencent_quote": {
                "provider": "tencent",
                "required": True,
                "call": lambda: {"sh000001": {"price": 3000}},
            }
        },
    )
    job_dir = tmp_path / "hot-money-context"
    _write_lock(str(job_dir), "run-orphan.json", age_seconds=1800)
    monkeypatch.setattr(provider_doctor, "cron_output_dir", lambda: str(tmp_path))

    report = provider_doctor.run_probes()

    assert report["status"] == "error"
    assert report["artifact_integrity"]["orphan_lock_count"] == 1
