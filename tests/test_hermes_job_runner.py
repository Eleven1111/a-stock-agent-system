"""Hermes job runner context isolation tests."""

import json
import os
import subprocess
import sys

from state_store import read_json
from runtime_context import (
    evaluate_dependencies,
    make_batch_id,
    output_has_signal,
    resolve_trading_date,
)


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _base_job(job_id, deliver="origin"):
    return {
        "id": job_id,
        "name": job_id,
        "schedule": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
        "command": f"python scripts/hermes_job_runner.py {job_id}",
        "cwd": ".",
        "enabled": True,
        "external": True,
        "expected_output": "json",
        "silent_when_no_signal": False,
        "execution_mode": "isolated_subprocess",
        "context_scope": "cron",
        "deliver": deliver,
        "max_output_chars": 2000,
        "context_from": [],
        "artifact_path_template": "{cron_output_dir}/{job_id}/{run_id}.json",
        "allowed_state_writes": [f"$HERMES_HOME/cron/output/{job_id}/"],
        "run": {"command": "", "cwd": ".", "timeout_seconds": 10},
    }


def test_runner_writes_artifact_and_ledger(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json\nprint(json.dumps({'schema':'demo_v1','alerts':[{'x':1}]}))\n",
        encoding="utf-8",
    )
    job = _base_job("demo")
    job["run"]["command"] = f"{sys.executable} {worker}"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")

    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / "hermes")
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "hermes_job_runner.py"), "demo", "--manifest", str(manifest)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0
    assert '"alerts": [{"x": 1}]' in result.stdout
    ledger = read_json(str(tmp_path / "hermes" / "cron" / "output" / "job_runs.json"), [])
    assert len(ledger) == 1
    artifact = read_json(ledger[0]["artifact_path"], {})
    assert artifact["schema"] == "hermes_cron_artifact_v2"
    assert artifact["job_id"] == "demo"
    assert artifact["has_signal"] is True
    assert artifact["summary"]["alerts_count"] == 1


def test_runner_local_delivery_suppresses_stdout_but_keeps_artifact(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text("print('{\"schema\":\"demo_v1\",\"message\":\"archived\"}')\n", encoding="utf-8")
    job = _base_job("local-demo", deliver="local")
    job["run"]["command"] = f"{sys.executable} {worker}"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")

    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / "hermes")
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "hermes_job_runner.py"), "local-demo", "--manifest", str(manifest)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    ledger = read_json(str(tmp_path / "hermes" / "cron" / "output" / "job_runs.json"), [])
    assert len(ledger) == 1
    assert os.path.exists(ledger[0]["artifact_path"])


def test_no_signal_detection_keeps_open_confirmations_visible():
    parsed = {
        "schema": "open_confirmation_v1",
        "signals": [],
        "confirmations": [{"code": "sz002156", "action": "not_buyable"}],
    }

    assert output_has_signal(parsed, "payload") is True


def test_dependency_gate_requires_successful_same_day_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    output = tmp_path / "hermes" / "cron" / "output" / "upstream"
    output.mkdir(parents=True)
    artifact = {
        "schema": "hermes_cron_artifact_v2",
        "job_id": "upstream",
        "run_id": "upstream-run",
        "batch_id": make_batch_id("2026-06-12"),
        "trading_date": "2026-06-12",
        "status": "ok",
        "finished_at": "2026-06-12T09:30:00+08:00",
        "summary": {"signals_count": 1},
    }
    (output / "upstream-run.json").write_text(json.dumps(artifact), encoding="utf-8")

    result = evaluate_dependencies(
        ["upstream"],
        trading_date="2026-06-12",
        batch_id=make_batch_id("2026-06-12"),
        now="2026-06-12T09:35:00+08:00",
    )

    assert result["passed"] is True
    assert result["dependencies"][0]["gate_status"] == "passed"


def test_dependency_gate_rejects_failed_or_stale_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    output = tmp_path / "hermes" / "cron" / "output" / "upstream"
    output.mkdir(parents=True)
    artifact = {
        "schema": "hermes_cron_artifact_v2",
        "job_id": "upstream",
        "run_id": "upstream-run",
        "batch_id": make_batch_id("2026-06-12"),
        "trading_date": "2026-06-12",
        "status": "failed",
        "finished_at": "2026-06-12T08:00:00+08:00",
        "summary": {},
    }
    (output / "upstream-run.json").write_text(json.dumps(artifact), encoding="utf-8")

    result = evaluate_dependencies(
        ["upstream"],
        trading_date="2026-06-12",
        batch_id=make_batch_id("2026-06-12"),
        policy={"max_age_minutes": 30},
        now="2026-06-12T09:35:00+08:00",
    )

    assert result["passed"] is False
    assert set(result["dependencies"][0]["reasons"]) == {"status_failed", "stale"}


def test_dependency_gate_selects_matching_artifact_instead_of_newer_other_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    output = tmp_path / "hermes" / "cron" / "output" / "upstream"
    output.mkdir(parents=True)
    matching = {
        "schema": "hermes_cron_artifact_v2",
        "job_id": "upstream",
        "run_id": "matching",
        "batch_id": make_batch_id("2026-06-12"),
        "trading_date": "2026-06-12",
        "status": "ok",
        "finished_at": "2026-06-12T09:30:00+08:00",
        "summary": {},
    }
    newer_other_batch = {
        **matching,
        "run_id": "other",
        "batch_id": make_batch_id("2026-06-15"),
        "trading_date": "2026-06-15",
        "finished_at": "2026-06-15T09:30:00+08:00",
    }
    matching_path = output / "matching.json"
    other_path = output / "other.json"
    matching_path.write_text(json.dumps(matching), encoding="utf-8")
    other_path.write_text(json.dumps(newer_other_batch), encoding="utf-8")
    os.utime(other_path, (matching_path.stat().st_mtime + 10, matching_path.stat().st_mtime + 10))

    result = evaluate_dependencies(
        ["upstream"],
        trading_date="2026-06-12",
        batch_id=make_batch_id("2026-06-12"),
        policy={"trading_date": "same_batch"},
        now="2026-06-12T09:35:00+08:00",
    )

    assert result["passed"] is True
    assert result["dependencies"][0]["run_id"] == "matching"


def test_weekend_run_belongs_to_latest_trading_date():
    assert resolve_trading_date("2026-06-14T10:00:00+08:00") == "2026-06-12"


def test_runner_blocks_without_starting_worker_when_required_dependency_missing(tmp_path):
    marker = tmp_path / "worker-ran"
    worker = tmp_path / "worker.py"
    worker.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    job = _base_job("downstream")
    job["context_from"] = ["missing-upstream"]
    job["run"]["command"] = f"{sys.executable} {worker}"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")

    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / "hermes")
    result = subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "scripts", "hermes_job_runner.py"),
            "downstream",
            "--manifest",
            str(manifest),
            "--trading-date",
            "2026-06-12",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 75
    assert marker.exists() is False
    ledger = read_json(str(tmp_path / "hermes" / "cron" / "output" / "job_runs.json"), [])
    artifact = read_json(ledger[0]["artifact_path"], {})
    assert artifact["status"] == "blocked"
    assert artifact["dependency_gate"]["passed"] is False
    assert artifact["trading_date"] == "2026-06-12"
    assert artifact["batch_id"] == make_batch_id("2026-06-12")
