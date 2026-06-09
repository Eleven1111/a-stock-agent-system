"""Hermes job runner context isolation tests."""

import json
import os
import subprocess
import sys

from state_store import read_json
from runtime_context import output_has_signal


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
    assert artifact["schema"] == "hermes_cron_artifact_v1"
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
