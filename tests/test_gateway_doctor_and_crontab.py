"""Deployment hardening helpers for Hermes Gateway cron failures."""

import sys

from scripts.generate_system_crontab import crontab_lines
from scripts.hermes_gateway_doctor import diagnose


def test_gateway_doctor_reports_source_run_agent_without_importing_it(tmp_path):
    hermes_home = tmp_path / "hermes"
    agent_dir = hermes_home / "hermes-agent"
    agent_dir.mkdir(parents=True)

    (agent_dir / "run_agent.py").write_text("# source\nclass Old: pass\n", encoding="utf-8")

    result = diagnose(str(hermes_home), str(agent_dir), sys.executable)

    assert result["schema"] == "hermes_gateway_doctor_v1"
    assert result["source_run_agent"]["lines"] == 2


def test_system_crontab_generation_uses_runner_only(tmp_path):
    manifest = {
        "jobs": [
            {
                "id": "demo",
                "schedule": "15 8 * * 1-5",
                "enabled": True,
                "command": "python scripts/run_agent_dag.py demo --emit-target",
            }
        ]
    }

    lines = crontab_lines(
        manifest,
        "/repo/a-stock",
        "/tmp/hermes",
        sys.executable,
        "/mnt/a-stock-state",
    )

    joined = "\n".join(lines)
    assert "HERMES_HOME=/tmp/hermes" in joined
    assert "A_STOCK_STATE_HOME=/mnt/a-stock-state" in joined
    assert "A_STOCK_RUNTIME=hermes" in joined
    assert "scripts/run_agent_dag.py demo --emit-target" in joined
    assert "15 8 * * 1-5" in joined


def test_system_crontab_skips_dependency_only_and_off_roles():
    manifest = {
        "jobs": [
            {
                "id": "scheduled-job",
                "schedule": "15 8 * * 1-5",
                "enabled": True,
                "role": "scheduled",
                "command": "python scripts/run_agent_dag.py scheduled-job --emit-target",
            },
            {
                "id": "dep-only",
                "schedule": "20 8 * * 1-5",
                "enabled": False,
                "role": "dependency_only",
                "command": "python scripts/run_agent_dag.py dep-only --emit-target",
            },
            {
                "id": "off-job",
                "schedule": "25 8 * * 1-5",
                "enabled": False,
                "role": "off",
                "command": "python scripts/run_agent_dag.py off-job --emit-target",
            },
        ]
    }

    joined = "\n".join(
        crontab_lines(manifest, "/repo", "/tmp/hermes", sys.executable, "/state")
    )

    assert "scheduled-job" in joined
    assert "dep-only" not in joined
    assert "off-job" not in joined


def test_system_crontab_rejects_template_jobs():
    manifest = {"jobs": [{"id": "bad", "schedule": "0 9 * * 1-5", "enabled": True, "command": "python x {code}"}]}

    try:
        crontab_lines(manifest, "/repo", "/tmp/hermes", "python")
    except ValueError as exc:
        assert "not self-contained" in str(exc)
    else:
        raise AssertionError("template job should be rejected")
