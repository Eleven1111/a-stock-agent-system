"""OpenClaw schedules deterministic command payloads instead of model turns."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from scripts import generate_openclaw_cron as cron_export
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


def test_origin_delivery_maps_to_explicit_announce_route(tmp_path):
    job = _job("target", 30)
    job["deliver"] = "origin"

    command = build_openclaw_commands(
        {"jobs": [job]},
        repo_dir=str(tmp_path),
        python="/venv/bin/python",
        delivery_channel="discord",
        delivery_to="user:test-recipient",
        delivery_account="test-account",
    )[0]
    parts = shlex.split(command)

    assert "--announce" in parts
    assert "--no-deliver" not in parts
    assert parts[parts.index("--channel") + 1] == "discord"
    assert parts[parts.index("--to") + 1] == "user:test-recipient"
    assert parts[parts.index("--account") + 1] == "test-account"
    assert parts[parts.index("--tz") + 1] == "Asia/Shanghai"


def test_origin_delivery_requires_explicit_deployment_target(tmp_path, monkeypatch):
    job = _job("target", 30)
    job["deliver"] = "origin"
    monkeypatch.delenv("A_STOCK_DELIVERY_TO", raising=False)

    with pytest.raises(ValueError, match="delivery target"):
        build_openclaw_commands(
            {"jobs": [job]},
            repo_dir=str(tmp_path),
            python="/venv/bin/python",
        )


def test_feishu_direct_delivery_skips_openclaw_announce(tmp_path):
    job = _job("target", 30)
    job["deliver"] = "feishu_direct"

    command = build_openclaw_commands(
        {"jobs": [job]},
        repo_dir=str(tmp_path),
        python="/venv/bin/python",
    )[0]
    parts = shlex.split(command)

    assert "--no-deliver" in parts
    assert "--announce" not in parts


def test_export_uses_configured_openclaw_binary(tmp_path):
    command = build_openclaw_commands(
        {"jobs": [_job("target", 30)]},
        repo_dir=str(tmp_path),
        python="/venv/bin/python",
        openclaw="/opt/openclaw/bin/openclaw",
    )[0]

    assert shlex.split(command)[0] == "/opt/openclaw/bin/openclaw"


def test_export_rejects_unknown_delivery_policy(tmp_path):
    job = _job("target", 30)
    job["deliver"] = "orgin"

    with pytest.raises(ValueError, match="unknown deliver policy"):
        build_openclaw_commands(
            {"jobs": [job]},
            repo_dir=str(tmp_path),
            python="/venv/bin/python",
        )


def test_reconcile_edits_existing_named_job_instead_of_creating_duplicate(tmp_path):
    job = _job("target", 30)
    job["deliver"] = "origin"

    command = build_openclaw_commands(
        {"jobs": [job]},
        repo_dir=str(tmp_path),
        python="/venv/bin/python",
        installed_jobs=[{"id": "cron-123", "name": "A-stock: target"}],
        delivery_to="user:test-recipient",
    )[0]
    parts = shlex.split(command)

    assert parts[:4] == ["openclaw", "cron", "edit", "cron-123"]
    assert "create" not in parts
    assert parts[parts.index("--cron") + 1] == "0 9 * * 1-5"
    assert "--announce" in parts


def test_reconcile_rejects_duplicate_installed_names(tmp_path):
    job = _job("target", 30)

    with pytest.raises(ValueError, match="duplicate installed OpenClaw jobs"):
        build_openclaw_commands(
            {"jobs": [job]},
            repo_dir=str(tmp_path),
            python="/venv/bin/python",
            installed_jobs=[
                {"id": "cron-1", "name": "A-stock: target"},
                {"id": "cron-2", "name": "A-stock: target"},
            ],
        )


def test_reconcile_loads_installed_jobs_from_openclaw_json(monkeypatch):
    completed = subprocess.CompletedProcess(
        ["openclaw", "cron", "list", "--json"],
        0,
        stdout=json.dumps({"jobs": [{"id": "cron-123", "name": "A-stock: target"}]}),
        stderr="",
    )
    monkeypatch.setattr(cron_export.subprocess, "run", lambda *args, **kwargs: completed)

    assert cron_export.load_installed_openclaw_jobs("openclaw") == [
        {"id": "cron-123", "name": "A-stock: target"}
    ]


def test_reconcile_surfaces_openclaw_list_failure(monkeypatch):
    completed = subprocess.CompletedProcess(
        ["openclaw", "cron", "list", "--json"],
        1,
        stdout="",
        stderr="gateway unavailable",
    )
    monkeypatch.setattr(cron_export.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(RuntimeError, match="gateway unavailable"):
        cron_export.load_installed_openclaw_jobs("openclaw")


@pytest.mark.parametrize("payload", ["not-json", '{"jobs":"invalid"}'])
def test_reconcile_rejects_invalid_openclaw_list_payload(monkeypatch, payload):
    completed = subprocess.CompletedProcess(
        ["openclaw", "cron", "list", "--json"],
        0,
        stdout=payload,
        stderr="",
    )
    monkeypatch.setattr(cron_export.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(RuntimeError, match="invalid JSON|jobs array"):
        cron_export.load_installed_openclaw_jobs("openclaw")


def test_apply_executes_generated_argv_without_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cron_export.subprocess,
        "run",
        lambda argv, check: calls.append((argv, check)),
    )

    cron_export.apply_openclaw_commands([
        "openclaw cron edit cron-123 --announce --channel discord",
    ])

    assert calls == [
        (["openclaw", "cron", "edit", "cron-123", "--announce", "--channel", "discord"], True)
    ]


def test_cli_reconcile_apply_edits_installed_job(tmp_path, monkeypatch):
    job = _job("target", 30)
    job["deliver"] = "origin"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    applied = []
    monkeypatch.setattr(
        cron_export,
        "load_installed_openclaw_jobs",
        lambda binary: [{"id": "cron-123", "name": "A-stock: target"}],
    )
    monkeypatch.setattr(
        cron_export,
        "apply_openclaw_commands",
        lambda commands: applied.extend(commands),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_openclaw_cron.py",
            "--manifest",
            str(manifest),
            "--repo-dir",
            str(tmp_path),
            "--python",
            "/venv/bin/python",
            "--state-home",
            "/state",
            "--delivery-to",
            "user:test-recipient",
            "--reconcile",
            "--apply",
        ],
    )

    assert cron_export.main() == 0
    assert len(applied) == 1
    assert shlex.split(applied[0])[:4] == ["openclaw", "cron", "edit", "cron-123"]


def test_repo_manifest_exports_every_enabled_job_as_command_cron():
    manifest = json.loads((ROOT / "cron" / "hermes-cron-manifest.json").read_text())
    commands = build_openclaw_commands(
        manifest,
        repo_dir=str(ROOT),
        python="/venv/bin/python",
        delivery_to="user:test-recipient",
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


def test_export_can_pin_env_file_without_embedding_secrets(tmp_path):
    command = build_openclaw_commands(
        {"jobs": [_job("target", 30)]},
        repo_dir=str(tmp_path),
        python="/venv/bin/python",
        env_file="/secure/a-stock.env",
    )[0]

    assert "A_STOCK_ENV_FILE=/secure/a-stock.env" in command
    assert "SERPER_API_KEY" not in command


def test_export_never_embeds_runtime_api_key_value(tmp_path, monkeypatch):
    sensitive_value = "synthetic-secret-value-for-redaction-test"
    monkeypatch.setenv("MIAOXIANG_API_KEY", sensitive_value)
    monkeypatch.setenv("A_STOCK_FEISHU_USER_ID", "private-user-id")

    command = build_openclaw_commands(
        {"jobs": [_job("target", 30)]},
        repo_dir=str(tmp_path),
        python="/venv/bin/python",
        env_file="/secure/a-stock.env",
    )[0]

    assert sensitive_value not in command
    assert "MIAOXIANG_API_KEY=" not in command
    assert "A_STOCK_FEISHU_USER_ID=" not in command
    assert "private-user-id" not in command
    assert "A_STOCK_ENV_FILE=/secure/a-stock.env" in command


def test_export_defaults_to_explicit_environment_state_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", "/shared/from-env")
    monkeypatch.setenv("A_STOCK_STATE_ID", "cluster-env")

    command = build_openclaw_commands(
        {"jobs": [_job("target", 30)]},
        repo_dir=str(tmp_path),
        python="/venv/bin/python",
    )[0]

    assert "A_STOCK_STATE_HOME=/shared/from-env" in command
    assert "A_STOCK_STATE_ID=cluster-env" in command


def test_cli_refuses_to_generate_unpinned_openclaw_jobs(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [_job("target", 30)]}), encoding="utf-8")
    env = os.environ.copy()
    env.pop("A_STOCK_STATE_HOME", None)
    env.pop("A_STOCK_STATE_ID", None)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "generate_openclaw_cron.py"),
            "--manifest",
            str(manifest),
            "--repo-dir",
            str(ROOT),
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "A_STOCK_STATE_HOME" in result.stderr
