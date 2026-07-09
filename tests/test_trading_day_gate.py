"""Trading-day scheduling must skip holidays and fail closed on calendar gaps."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from state_store import read_json
from trading_day_gate import evaluate_job_trading_day
from scripts.run_agent_dag import execute_dag


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _job(policy: str = "required") -> dict:
    return {
        "id": "gate-demo",
        "name": "gate-demo",
        "schedule": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
        "command": "python scripts/run_agent_dag.py gate-demo --emit-target",
        "cwd": ".",
        "enabled": True,
        "external": True,
        "expected_output": "json",
        "silent_when_no_signal": True,
        "execution_mode": "isolated_subprocess",
        "context_scope": "cron",
        "deliver": "local",
        "max_output_chars": 1000,
        "context_from": [],
        "trading_day_policy": policy,
        "artifact_path_template": "{cron_output_dir}/{job_id}/{run_id}.json",
        "allowed_state_writes": ["$A_STOCK_STATE_HOME/cron/output/gate-demo/"],
        "run": {
            "command": f"{sys.executable} -c \"print('should-not-run')\"",
            "cwd": ".",
            "timeout_seconds": 10,
        },
    }


def test_gate_skips_closed_day_and_blocks_uncovered_calendar():
    assert evaluate_job_trading_day(_job(), "2026-06-19")["action"] == "skip"

    uncovered = evaluate_job_trading_day(_job(), "2027-01-04")
    assert uncovered["action"] == "block"
    assert uncovered["reason"] == "calendar_uncovered"


def test_calendar_day_job_is_not_subject_to_market_calendar():
    result = evaluate_job_trading_day(_job("calendar_day"), "2027-01-04")
    assert result == {
        "action": "run",
        "calendar_date": "2027-01-04",
        "policy": "calendar_day",
        "reason": None,
    }


def test_runner_persists_silent_skip_without_starting_worker(tmp_path):
    marker = tmp_path / "worker-ran"
    worker = tmp_path / "worker.py"
    worker.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    job = _job()
    job["run"]["command"] = f"{sys.executable} {worker}"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    env = os.environ.copy()
    env["A_STOCK_STATE_HOME"] = str(tmp_path / "state")

    result = subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "scripts", "agent_job_runner.py"),
            "gate-demo",
            "--manifest",
            str(manifest),
            "--runtime",
            "openclaw",
            "--calendar-date",
            "2026-06-19",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert marker.exists() is False
    ledger = read_json(str(tmp_path / "state" / "cron" / "output" / "job_runs.json"), [])
    artifact = read_json(ledger[0]["artifact_path"], {})
    assert artifact["status"] == "skipped_non_trading_day"
    assert artifact["calendar_gate"]["action"] == "skip"


def test_runner_blocks_when_calendar_is_uncovered(tmp_path):
    job = _job()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    env = os.environ.copy()
    env["A_STOCK_STATE_HOME"] = str(tmp_path / "state")

    result = subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "scripts", "agent_job_runner.py"),
            "gate-demo",
            "--manifest",
            str(manifest),
            "--runtime",
            "openclaw",
            "--calendar-date",
            "2027-01-04",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 75
    ledger = read_json(str(tmp_path / "state" / "cron" / "output" / "job_runs.json"), [])
    artifact = read_json(ledger[0]["artifact_path"], {})
    assert artifact["status"] == "blocked_calendar"
    assert artifact["calendar_gate"]["reason"] == "calendar_uncovered"


def test_dag_skip_keeps_latest_trading_date_batch(tmp_path):
    job = _job()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    state = tmp_path / "state"

    result = execute_dag(
        manifest_path=str(manifest),
        targets=["gate-demo"],
        trading_date="2026-06-19",
        runtime="local",
        env={**os.environ, "A_STOCK_STATE_HOME": str(state)},
    )

    assert result["status"] == "skipped_non_trading_day"
    assert result["trading_date"] == "2026-06-18"
    assert result["batch_id"] == "a-share-20260618"


def test_dag_calendar_block_is_reported_as_blocked(tmp_path):
    job = _job()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    state = tmp_path / "state"

    result = execute_dag(
        manifest_path=str(manifest),
        targets=["gate-demo"],
        trading_date="2027-01-04",
        runtime="local",
        env={**os.environ, "A_STOCK_STATE_HOME": str(state)},
    )

    assert result["status"] == "blocked"
    assert result["runs"][0]["status"] == "blocked_calendar"
    assert result["runs"][0]["returncode"] == 75


def test_dag_openclaw_without_explicit_state_home_fails_closed(tmp_path):
    """Fail-closed: openclaw must not silently fall back to a repo/default home.

    The identity gate now blocks before any calendar evaluation when no explicit
    A_STOCK_STATE_HOME is configured, instead of minting a fresh identity.
    """
    job = _job()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")

    result = execute_dag(
        manifest_path=str(manifest),
        targets=["gate-demo"],
        trading_date="2026-06-19",
        runtime="openclaw",
        env={"HOME": str(tmp_path)},
    )

    assert result["status"] == "blocked"
    assert result["runs"][0]["status"] == "blocked_state"
    assert result["runs"][0]["returncode"] == 78


def test_dag_openclaw_with_explicit_state_home_reaches_calendar_skip(tmp_path):
    job = _job()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    state = tmp_path / "state"

    result = execute_dag(
        manifest_path=str(manifest),
        targets=["gate-demo"],
        trading_date="2026-06-19",
        runtime="openclaw",
        env={"HOME": str(tmp_path), "A_STOCK_STATE_HOME": str(state)},
    )

    assert result["status"] == "skipped_non_trading_day"
    assert result["runs"][0]["status"] == "skipped_non_trading_day"


def test_runner_dry_run_has_no_state_side_effects_on_closed_day(tmp_path):
    job = _job()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    state = tmp_path / "state"
    env = {**os.environ, "A_STOCK_STATE_HOME": str(state)}

    result = subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "scripts", "agent_job_runner.py"),
            "gate-demo",
            "--manifest",
            str(manifest),
            "--runtime",
            "openclaw",
            "--calendar-date",
            "2026-06-19",
            "--dry-run",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["calendar_gate"]["action"] == "skip"
    assert not state.exists()
