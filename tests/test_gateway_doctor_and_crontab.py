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
                "command": "python scripts/agent_job_runner.py demo",
            }
        ]
    }

    lines = crontab_lines(manifest, "/repo/a-stock", "/tmp/hermes", sys.executable)

    joined = "\n".join(lines)
    assert "HERMES_HOME=/tmp/hermes" in joined
    assert "scripts/agent_job_runner.py demo" in joined
    assert "15 8 * * 1-5" in joined


def test_system_crontab_rejects_template_jobs():
    manifest = {"jobs": [{"id": "bad", "schedule": "0 9 * * 1-5", "enabled": True, "command": "python x {code}"}]}

    try:
        crontab_lines(manifest, "/repo", "/tmp/hermes", "python")
    except ValueError as exc:
        assert "not self-contained" in str(exc)
    else:
        raise AssertionError("template job should be rejected")
