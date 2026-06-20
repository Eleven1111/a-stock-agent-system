"""OpenClaw schedules deterministic command payloads instead of model turns."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.generate_openclaw_cron import build_openclaw_commands, dependency_timeout_budget


ROOT = Path(__file__).resolve().parents[1]


def _job(job_id: str, timeout: int, dependencies: list[str] | None = None) -> dict:
    return {
        "id": job_id,
        "name": job_id,
        "schedule": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
        "enabled": True,
        "deliver": "local",
        "context_from": dependencies or [],
        "run": {"timeout_seconds": timeout},
    }


def test_timeout_budget_includes_required_dependency_closure_and_retry():
    jobs = {
        "upstream": _job("upstream", 20),
        "target": _job("target", 30, ["upstream"]),
    }
    jobs["upstream"]["retry_policy"] = {"max_attempts": 2}

    assert dependency_timeout_budget(jobs, "target", grace_seconds=60) == 160


def test_export_uses_openclaw_command_payload_without_model_prompt(tmp_path):
    manifest = {
        "jobs": [_job("target", 30)],
    }
    commands = build_openclaw_commands(
        manifest,
        repo_dir=str(tmp_path),
        python="/venv/bin/python",
    )
    command = commands[0]

    assert "--command-argv" in command
    assert "--message" not in command
    assert "--model" not in command
    assert "--no-deliver" in command
    assert "--timeout-seconds 120" in command
    assert '"--runtime", "openclaw"' in command


def test_repo_manifest_exports_every_enabled_job_as_command_cron():
    manifest = json.loads((ROOT / "cron" / "hermes-cron-manifest.json").read_text())
    commands = build_openclaw_commands(
        manifest,
        repo_dir=str(ROOT),
        python="/venv/bin/python",
    )

    enabled = [job for job in manifest["jobs"] if job.get("enabled", True)]
    assert len(commands) == len(enabled)
    assert all("--command-argv" in command for command in commands)
    assert all("--message" not in command for command in commands)


def test_export_can_pin_shared_state_identity(tmp_path):
    command = build_openclaw_commands(
        {"jobs": [_job("target", 30)]},
        repo_dir=str(tmp_path),
        python="/venv/bin/python",
        state_home="/shared/a-stock",
        state_id="cluster-1",
    )[0]

    assert "A_STOCK_STATE_HOME=/shared/a-stock" in command
    assert "A_STOCK_STATE_ID=cluster-1" in command
