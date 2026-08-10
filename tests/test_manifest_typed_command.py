"""Typed command contract (T03).

The scheduler used to hand manifest strings to a shell. These tests pin the
replacement: enabled jobs are argv arrays executed with ``shell=False``, the
validator rejects every shell construct rather than executing it, and anything
unresolvable — a missing binary, a cwd outside the repository — fails closed at
launch instead of producing a confusing runtime error.
"""

import json
import os
import subprocess
import sys
import tempfile

import pytest

import manifest_command
from scripts import cron_dispatch
from scripts.validate_cron_manifest import validate


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MANIFEST_PATH = os.path.join(ROOT, "cron", "hermes-cron-manifest.json")


def _base_job(**overrides):
    job = {
        "id": "typed-job",
        "name": "typed",
        "schedule": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
        "command_argv": ["python", "scripts/run_agent_dag.py", "typed-job", "--emit-target"],
        "cwd": ".",
        "enabled": True,
        "external": True,
        "expected_output": "json",
        "silent_when_no_signal": True,
        "execution_mode": "isolated_subprocess",
        "context_scope": "cron",
        "deliver": "local",
        "max_output_chars": 2000,
        "context_from": [],
        "artifact_path_template": "{cron_output_dir}/{job_id}/{run_id}.json",
        "allowed_state_writes": ["$A_STOCK_STATE_HOME/cron/output/typed-job/"],
        "run": {
            "argv": ["python", "skills/stock-triage/scripts/context_digest.py", "--json"],
            "cwd": ".",
            "timeout_seconds": 10,
            "timeout_tier": "short",
        },
    }
    job.update(overrides)
    return job


def _validate(job):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
        json.dump({"jobs": [job]}, handle)
        path = handle.name
    try:
        return validate(path)
    finally:
        os.unlink(path)


# --------------------------------------------------------------------------
# repository manifest
# --------------------------------------------------------------------------


def test_every_enabled_repo_job_uses_typed_argv():
    with open(MANIFEST_PATH, encoding="utf-8") as handle:
        manifest = json.load(handle)

    enabled = [job for job in manifest["jobs"] if job.get("enabled")]
    assert len(enabled) == 48

    for job in enabled:
        assert isinstance(job.get("command_argv"), list) and job["command_argv"]
        assert isinstance(job["run"].get("argv"), list) and job["run"]["argv"]
        assert "command" not in job, f"{job['id']} still carries a shell string"
        assert "command" not in job["run"], f"{job['id']} still carries a run shell string"


def test_no_production_shell_execution_remains():
    """`shell=True` must not survive on any scheduled execution path."""
    for path in ("scripts/cron_dispatch.py", "scripts/hermes_job_runner.py"):
        source = open(os.path.join(ROOT, path), encoding="utf-8").read()
        assert "shell=True" not in source, f"{path} still executes through a shell"


# --------------------------------------------------------------------------
# validator rejections
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argument",
    ["a|b", "a>b", "a<b", "$HOME", "`id`", "a;b", "a&b", "*.json"],
)
def test_validator_rejects_shell_constructs_in_argv(argument):
    job = _base_job()
    job["run"]["argv"] = ["python", "script.py", argument]

    assert _validate(job) is False


def test_validator_rejects_shell_string_on_enabled_job():
    job = _base_job()
    job["command"] = "python scripts/run_agent_dag.py typed-job --emit-target"

    assert _validate(job) is False


def test_validator_rejects_run_shell_string_on_enabled_job():
    job = _base_job()
    job["run"]["command"] = "python script.py"

    assert _validate(job) is False


def test_validator_accepts_shell_string_on_disabled_job():
    """One migration release of compatibility, and only while disabled."""
    job = _base_job(enabled=False)
    job.pop("command_argv")
    job["command"] = "python scripts/run_agent_dag.py typed-job --emit-target"
    job["run"] = {
        "command": "python script.py --json",
        "cwd": ".",
        "timeout_seconds": 10,
        "timeout_tier": "short",
    }

    assert _validate(job) is True


def test_validator_rejects_undeclared_template_variables():
    job = _base_job()
    job["run"]["argv"] = ["python", "script.py", "{code}"]

    assert _validate(job) is False


def test_validator_requires_dag_entry_for_enabled_external_jobs():
    job = _base_job()
    job["command_argv"] = ["python", "skills/stock-triage/scripts/intraday_monitor.py"]

    assert _validate(job) is False


def test_validator_rejects_recursive_runner_invocation():
    job = _base_job()
    job["run"]["argv"] = ["python", "scripts/agent_job_runner.py", "typed-job"]

    assert _validate(job) is False


# --------------------------------------------------------------------------
# resolution helpers
# --------------------------------------------------------------------------


def test_whole_value_substitution_cannot_add_arguments():
    argv = manifest_command.substitute_argv(
        ["python", "script.py", "{code}"], {"code": "--danger extra"}
    )

    assert argv == ["python", "script.py", "--danger extra"]
    assert len(argv) == 3


def test_fragment_substitution_requires_explicit_declaration():
    with pytest.raises(manifest_command.CommandContractError):
        manifest_command.substitute_argv(["--code={code}"], {"code": "600519"})

    assert manifest_command.substitute_argv(
        ["--code={code}"], {"code": "600519"}, allow_fragments=["code"]
    ) == ["--code=600519"]


def test_missing_executable_fails_closed():
    with pytest.raises(manifest_command.CommandContractError):
        manifest_command.resolve_executable(["definitely-not-a-real-binary-xyz"])


def test_cwd_outside_the_repository_fails_closed(tmp_path):
    with pytest.raises(manifest_command.CommandContractError):
        manifest_command.resolve_cwd(ROOT, "../..")

    with pytest.raises(manifest_command.CommandContractError):
        manifest_command.resolve_cwd(ROOT, "no/such/directory")

    assert manifest_command.resolve_cwd(ROOT, ".") == ROOT


def test_enabled_job_without_argv_fails_closed():
    with pytest.raises(manifest_command.CommandContractError):
        manifest_command.business_argv({"id": "x", "enabled": True, "run": {"command": "python x.py"}})

    assert manifest_command.business_argv(
        {"id": "x", "enabled": False, "run": {"command": "python x.py"}}
    ) == ["python", "x.py"]


# --------------------------------------------------------------------------
# dispatcher execution boundary
# --------------------------------------------------------------------------


def test_dispatcher_refuses_a_job_without_typed_argv(tmp_path, capsys):
    pid = cron_dispatch.launch(
        {"id": "legacy", "command": "python -c \"print(1)\"", "cwd": "."},
        log_path=str(tmp_path / "jobs.log"),
    )

    assert pid is None
    assert "command_argv" in capsys.readouterr().out


def test_dispatcher_refuses_shell_metacharacters(tmp_path, capsys):
    pid = cron_dispatch.launch(
        {"id": "piped", "command_argv": ["python", "-c", "print(1)", "|", "tee", "x"], "cwd": "."},
        log_path=str(tmp_path / "jobs.log"),
    )

    assert pid is None
    assert "metacharacter" in capsys.readouterr().out


def test_dispatcher_refuses_cwd_outside_the_repository(tmp_path, capsys):
    pid = cron_dispatch.launch(
        {"id": "escape", "command_argv": ["python", "-c", "print(1)"], "cwd": "../.."},
        log_path=str(tmp_path / "jobs.log"),
    )

    assert pid is None
    assert "escapes the repository" in capsys.readouterr().out


def test_dispatcher_refuses_a_missing_executable(tmp_path, capsys):
    pid = cron_dispatch.launch(
        {"id": "missing", "command_argv": ["definitely-not-a-real-binary-xyz"], "cwd": "."},
        log_path=str(tmp_path / "jobs.log"),
    )

    assert pid is None
    assert "executable not found" in capsys.readouterr().out


def test_argv_arguments_survive_spaces_without_quoting(tmp_path):
    """The regression argv exists to prevent: a space-bearing argument."""
    marker = tmp_path / "out with space.txt"
    script = tmp_path / "writer.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('ok')\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(script), str(marker)],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert marker.read_text(encoding="utf-8") == "ok"
