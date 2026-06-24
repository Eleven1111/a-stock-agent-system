"""Runtime-neutral job runner context isolation tests."""

import json
import os
import subprocess
import sys

import pytest

from scripts import hermes_job_runner as job_runner
from scripts import run_agent_dag
from state_store import read_json
from runtime_context import (
    evaluate_dependencies,
    make_batch_id,
    output_has_signal,
    resolve_trading_date,
)


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_openclaw_runtime_env_preserves_default_state_fallback(tmp_path, monkeypatch):
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("A_STOCK_ENV_FILE", str(env_file))
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.delenv("A_STOCK_STATE_ID", raising=False)

    run_env = job_runner.build_runtime_env("openclaw")

    assert run_env["A_STOCK_STATE_HOME"] == job_runner.ROOT
    assert run_env["A_STOCK_STATE_ID"] == "default"


def test_runtime_env_loads_explicit_env_file_for_isolated_jobs(tmp_path, monkeypatch):
    env_file = tmp_path / "a-stock.env"
    env_file.write_text("SERPER_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("A_STOCK_ENV_FILE", str(env_file))
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    run_env = job_runner.build_runtime_env("openclaw")

    assert run_env["SERPER_API_KEY"] == "from-file"


def test_dag_target_output_respects_delivery_and_silent_contract():
    artifact = {"stdout": '{"status":"no_signal","signals":[]}', "has_signal": False}

    assert run_agent_dag.target_output(
        {"deliver": "origin", "silent_when_no_signal": True}, artifact
    ) == "NO_REPLY\n"
    assert run_agent_dag.target_output(
        {"deliver": "local", "silent_when_no_signal": False},
        {**artifact, "stdout": "large local payload", "has_signal": True},
    ) == "NO_REPLY\n"


@pytest.mark.parametrize(
    "status",
    ["insufficient_data", "stale_data", "degraded", "failed", "blocked", "timeout"],
)
def test_operational_failures_are_not_suppressed_as_no_signal(status):
    parsed = {"status": status, "signals": [], "message": "provider unavailable"}

    assert output_has_signal(parsed, json.dumps(parsed)) is True


def test_true_no_signal_remains_silent():
    parsed = {"status": "no_signal", "signals": []}

    assert output_has_signal(parsed, json.dumps(parsed)) is False


def test_dag_target_output_emits_operational_failure_for_origin_delivery():
    stdout = '{"status":"insufficient_data","signals":[],"message":"provider unavailable"}'

    assert run_agent_dag.target_output(
        {"deliver": "origin", "silent_when_no_signal": True},
        {"stdout": stdout, "has_signal": True},
    ) == stdout + "\n"


def test_dag_target_output_compacts_payload_before_openclaw_output_limit():
    output = run_agent_dag.target_output(
        {
            "id": "news-monitor",
            "deliver": "origin",
            "silent_when_no_signal": False,
            "max_output_chars": 300,
        },
        {
            "status": "ok",
            "stdout": "x" * 5_000,
            "has_signal": True,
            "summary": {
                "status": "stale_data",
                "events_count": 37,
                "signals_count": 0,
            },
        },
    )

    assert len(output) <= 301
    assert "已压缩" in output  # compacted, not the raw 5000-char payload
    assert "news-monitor" in output
    assert "stale_data" in output



def test_dry_run_replaces_bare_python_with_current_interpreter(
    tmp_path,
    monkeypatch,
    capsys,
):
    worker = tmp_path / "worker.py"
    worker.write_text("print('ok')\n", encoding="utf-8")
    job = _base_job("python-path")
    job["run"]["command"] = f"python {worker}"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))

    result = job_runner.run_job(
        job_runner.build_parser().parse_args([
            "python-path",
            "--manifest",
            str(manifest),
            "--dry-run",
        ])
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["command"].startswith(sys.executable + " ")


def _base_job(job_id, deliver="origin"):
    return {
        "id": job_id,
        "name": job_id,
        "schedule": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
        "command": f"python scripts/agent_job_runner.py {job_id}",
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
        "trading_day_policy": "calendar_day",
        "artifact_path_template": "{cron_output_dir}/{job_id}/{run_id}.json",
        "allowed_state_writes": [f"$HERMES_HOME/cron/output/{job_id}/"],
        "run": {"command": "", "cwd": ".", "timeout_seconds": 10},
    }


def test_openclaw_runner_writes_artifact_ledger_and_snapshot(tmp_path):
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
    env["A_STOCK_STATE_HOME"] = str(tmp_path / "state")
    result = subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "scripts", "agent_job_runner.py"),
            "demo",
            "--manifest",
            str(manifest),
            "--runtime",
            "openclaw",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0
    assert '"alerts": [{"x": 1}]' in result.stdout
    ledger = read_json(str(tmp_path / "state" / "cron" / "output" / "job_runs.json"), [])
    assert len(ledger) == 1
    artifact = read_json(ledger[0]["artifact_path"], {})
    assert artifact["schema"] == "hermes_cron_artifact_v2"
    assert artifact["job_id"] == "demo"
    assert artifact["runtime"] == "openclaw"
    assert artifact["has_signal"] is True
    assert artifact["summary"]["alerts_count"] == 1
    assert artifact["market_snapshot"]["schema"] == "market_snapshot_v1"
    assert os.path.exists(artifact["market_snapshot"]["snapshot_path"])


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
