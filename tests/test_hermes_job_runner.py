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
    summarize_output,
    resolve_trading_date,
)


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_runtime_env_does_not_fabricate_repo_state_home(tmp_path, monkeypatch):
    """The runner must never silently make the repo a state root (split-brain)."""
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("A_STOCK_ENV_FILE", str(env_file))
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.delenv("A_STOCK_STATE_ID", raising=False)

    run_env = job_runner.build_runtime_env("openclaw")

    assert run_env.get("A_STOCK_STATE_HOME") != job_runner.ROOT
    assert run_env.get("A_STOCK_STATE_ID") != "default"


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
    job["run"]["argv"] = ["python", str(worker)]
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


def _seed_adaptive_state(state_home, job_id, *, miss_streak, ticks_since_run):
    path = state_home / "runtime" / "adaptive_schedule.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema": "adaptive_schedule_v1",
            "jobs": {
                job_id: {
                    "miss_streak": miss_streak,
                    "ticks_since_run": ticks_since_run,
                }
            },
        }),
        encoding="utf-8",
    )
    return path


def test_adaptive_backoff_enforce_mode_skips_when_not_due(tmp_path, monkeypatch):
    marker = tmp_path / "worker-ran"
    worker = tmp_path / "worker.py"
    worker.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n", encoding="utf-8")
    job = _base_job("official-policy-watch")
    job["adaptive_backoff"] = True
    job["run"]["argv"] = [sys.executable, str(worker)]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    state_home = tmp_path / "state"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state_home))
    monkeypatch.delenv("A_STOCK_STATE_ID", raising=False)
    # streak=21 -> interval=8; ticks_since_run starts at 5, should_run bumps to 6 < 8 -> not due.
    _seed_adaptive_state(state_home, "official-policy-watch", miss_streak=21, ticks_since_run=5)
    monkeypatch.setattr(
        job_runner.delivery_policy,
        "load_policy",
        lambda *a, **k: {"adaptive_backoff": {"enabled": True, "mode": "enforce"}},
    )

    result = job_runner.run_job(
        job_runner.build_parser().parse_args(["official-policy-watch", "--manifest", str(manifest)])
    )

    assert result == 0
    assert not marker.exists()
    ledger = read_json(str(state_home / "cron" / "output" / "job_runs.json"), [])
    artifact = read_json(ledger[0]["artifact_path"], {})
    assert artifact["status"] == "skipped_adaptive_backoff"
    assert artifact["adaptive_schedule"]["would_skip"] is True


def test_adaptive_backoff_shadow_mode_still_runs_when_not_due(tmp_path, monkeypatch):
    marker = tmp_path / "worker-ran"
    worker = tmp_path / "worker.py"
    worker.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n", encoding="utf-8")
    job = _base_job("news-monitor")
    job["adaptive_backoff"] = True
    job["run"]["argv"] = [sys.executable, str(worker)]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    state_home = tmp_path / "state"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state_home))
    monkeypatch.delenv("A_STOCK_STATE_ID", raising=False)
    _seed_adaptive_state(state_home, "news-monitor", miss_streak=21, ticks_since_run=5)
    # Real shipped default is shadow -- no monkeypatch of delivery_policy here.

    result = job_runner.run_job(
        job_runner.build_parser().parse_args(["news-monitor", "--manifest", str(manifest)])
    )

    assert result == 0
    assert marker.exists()


def test_adaptive_backoff_records_outcome_after_successful_run(tmp_path, monkeypatch):
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json\nprint(json.dumps({'schema':'demo_v1','alerts':[{'x':1}]}))\n",
        encoding="utf-8",
    )
    job = _base_job("news-monitor-intraday")
    job["adaptive_backoff"] = True
    job["run"]["argv"] = [sys.executable, str(worker)]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    state_home = tmp_path / "state"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state_home))
    monkeypatch.delenv("A_STOCK_STATE_ID", raising=False)
    _seed_adaptive_state(state_home, "news-monitor-intraday", miss_streak=5, ticks_since_run=0)

    result = job_runner.run_job(
        job_runner.build_parser().parse_args(["news-monitor-intraday", "--manifest", str(manifest)])
    )

    assert result == 0
    state = read_json(str(state_home / "runtime" / "adaptive_schedule.json"), {})
    entry = state["jobs"]["news-monitor-intraday"]
    assert entry["miss_streak"] == 0
    assert entry["ticks_since_run"] == 0


def _base_job(job_id, deliver="origin"):
    return {
        "id": job_id,
        "name": job_id,
        "schedule": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
        "command_argv": ["python", "scripts/agent_job_runner.py", job_id],
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
        "run": {"argv": [sys.executable, "-c", "pass"], "cwd": ".", "timeout_seconds": 10},
    }


def test_openclaw_runner_writes_artifact_ledger_and_snapshot(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json\nprint(json.dumps({'schema':'demo_v1','alerts':[{'x':1}]}))\n",
        encoding="utf-8",
    )
    job = _base_job("demo")
    job["run"]["argv"] = [sys.executable, str(worker)]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")

    env = os.environ.copy()
    env["A_STOCK_STATE_HOME"] = str(tmp_path / "state")
    env.pop("A_STOCK_STATE_ID", None)
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
    job["run"]["argv"] = [sys.executable, str(worker)]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")

    env = os.environ.copy()
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它并会被 os.environ.copy() 继承下来；子进程要测的是
    # HERMES_HOME 驱动的路径，必须先弹出继承来的 A_STOCK_STATE_HOME。
    env.pop("A_STOCK_STATE_HOME", None)
    env["HERMES_HOME"] = str(tmp_path / "hermes")
    env.pop("A_STOCK_STATE_ID", None)
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


def test_runner_feishu_direct_delivery_suppresses_stdout_and_never_calls_lark_cli(tmp_path, monkeypatch):
    worker = tmp_path / "worker.py"
    worker.write_text("print('{\"schema\":\"demo_v1\",\"message\":\"routine\"}')\n", encoding="utf-8")
    job = _base_job("feishu-demo", deliver="feishu_direct")
    job["run"]["argv"] = [sys.executable, str(worker)]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")

    env = os.environ.copy()
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它并会被 os.environ.copy() 继承下来；子进程要测的是
    # HERMES_HOME 驱动的路径，必须先弹出继承来的 A_STOCK_STATE_HOME。
    env.pop("A_STOCK_STATE_HOME", None)
    env["HERMES_HOME"] = str(tmp_path / "hermes")
    env.pop("A_STOCK_FEISHU_CHAT_ID", None)
    env.pop("A_STOCK_STATE_ID", None)
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "hermes_job_runner.py"), "feishu-demo", "--manifest", str(manifest)],
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
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
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
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
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
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
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


def test_timeout_after_worker_wrote_stderr_still_lands_a_timeout_artifact(
    tmp_path,
    monkeypatch,
):
    """Regression for issue #159: the timeout handler must not crash the runner.

    ``subprocess.run(text=True, timeout=...)`` returns decoded output on the
    happy path, but ``TimeoutExpired.stdout/.stderr`` come back as raw bytes.
    Concatenating those with a str raised TypeError inside the except branch, so
    the runner died before writing anything: no artifact, no ``job.finished`` —
    the ghost failure that made every downstream gate see "never ran".

    The worker writes stderr *before* sleeping on purpose; without that byte the
    handler gets ``None`` and the bug stays invisible.
    """
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import sys, time\n"
        "sys.stderr.write('warming up\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    job = _base_job("slow-worker")
    job["run"]["argv"] = [sys.executable, str(worker)]
    job["run"]["timeout_seconds"] = 1
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    state_home = tmp_path / "state"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state_home))
    monkeypatch.delenv("A_STOCK_STATE_ID", raising=False)

    result = job_runner.run_job(
        job_runner.build_parser().parse_args(["slow-worker", "--manifest", str(manifest)])
    )

    assert result == 124
    ledger = read_json(str(state_home / "cron" / "output" / "job_runs.json"), [])
    assert len(ledger) == 1
    artifact = read_json(ledger[0]["artifact_path"], {})
    assert artifact["status"] == "timeout"
    assert "TIMEOUT after 1s" in artifact["stderr"]
    assert "warming up" in artifact["stderr"]


def test_runner_crash_still_writes_a_terminal_artifact(tmp_path, monkeypatch):
    """Any runner-internal error must remain visible as a failed artifact."""
    job = _base_job("crashy")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    state_home = tmp_path / "state"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state_home))
    monkeypatch.delenv("A_STOCK_STATE_ID", raising=False)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated runner defect")

    # The job is spawned through run_isolated (own process group) rather than
    # subprocess.run, so that is where a runner-internal defect now originates.
    monkeypatch.setattr(job_runner, "run_isolated", _boom)

    result = job_runner.run_job(
        job_runner.build_parser().parse_args(["crashy", "--manifest", str(manifest)])
    )

    assert result == 1
    ledger = read_json(str(state_home / "cron" / "output" / "job_runs.json"), [])
    assert len(ledger) == 1
    artifact = read_json(ledger[0]["artifact_path"], {})
    assert artifact["status"] == "failed"
    assert "simulated runner defect" in artifact["stderr"]


def test_manifest_run_env_reaches_worker_without_overriding_reserved_keys(
    tmp_path,
    monkeypatch,
):
    """Per-job env travels with the manifest; runner-owned identity does not."""
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, os\n"
        "print(json.dumps({\n"
        "    'schema': 'demo_v1',\n"
        "    'skip': os.environ.get('A_STOCK_SKIP_INPUT_SNAPSHOT'),\n"
        "    'state_home': os.environ.get('A_STOCK_STATE_HOME'),\n"
        "    'job_id': os.environ.get('A_STOCK_JOB_ID'),\n"
        "    'lowercase': os.environ.get('a_stock_lowercase'),\n"
        "}))\n",
        encoding="utf-8",
    )
    job = _base_job("env-demo")
    job["run"]["argv"] = [sys.executable, str(worker)]
    job["run"]["env"] = {
        "A_STOCK_SKIP_INPUT_SNAPSHOT": "1",
        "A_STOCK_STATE_HOME": "/tmp/hijacked-state-home",
        "A_STOCK_JOB_ID": "forged",
        "a_stock_lowercase": "rejected",
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    state_home = tmp_path / "state"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state_home))
    monkeypatch.delenv("A_STOCK_STATE_ID", raising=False)

    result = job_runner.run_job(
        job_runner.build_parser().parse_args(["env-demo", "--manifest", str(manifest)])
    )

    assert result == 0
    ledger = read_json(str(state_home / "cron" / "output" / "job_runs.json"), [])
    payload = json.loads(read_json(ledger[0]["artifact_path"], {})["stdout"])
    assert payload["skip"] == "1"
    assert payload["state_home"] == str(state_home)
    assert payload["job_id"] == "env-demo"
    assert payload["lowercase"] is None


def test_runner_maps_business_block_returncode_to_blocked_artifact(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, sys\n"
        "print(json.dumps({'schema':'demo_v1','status':'insufficient_data'}))\n"
        "raise SystemExit(75)\n",
        encoding="utf-8",
    )
    job = _base_job("blocked-business")
    job["run"]["argv"] = [sys.executable, str(worker)]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")

    env = os.environ.copy()
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它并会被 os.environ.copy() 继承下来；子进程要测的是
    # HERMES_HOME 驱动的路径，必须先弹出继承来的 A_STOCK_STATE_HOME。
    env.pop("A_STOCK_STATE_HOME", None)
    env["HERMES_HOME"] = str(tmp_path / "hermes")
    env.pop("A_STOCK_STATE_ID", None)
    result = subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "scripts", "hermes_job_runner.py"),
            "blocked-business",
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
    ledger = read_json(str(tmp_path / "hermes" / "cron" / "output" / "job_runs.json"), [])
    artifact = read_json(ledger[0]["artifact_path"], {})
    assert artifact["status"] == "blocked"
    assert artifact["summary"]["status"] == "insufficient_data"


def test_runner_blocks_without_starting_worker_when_required_dependency_missing(tmp_path):
    marker = tmp_path / "worker-ran"
    worker = tmp_path / "worker.py"
    worker.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    job = _base_job("downstream")
    job["context_from"] = ["missing-upstream"]
    job["run"]["argv"] = [sys.executable, str(worker)]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")

    env = os.environ.copy()
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它并会被 os.environ.copy() 继承下来；子进程要测的是
    # HERMES_HOME 驱动的路径，必须先弹出继承来的 A_STOCK_STATE_HOME。
    env.pop("A_STOCK_STATE_HOME", None)
    env["HERMES_HOME"] = str(tmp_path / "hermes")
    env.pop("A_STOCK_STATE_ID", None)
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


def test_summary_keeps_counters_from_payloads_outside_the_whitelist():
    """白名单之外的键名会让 summary 塌成只剩 schema —— 运维面上"绿的、有信号"，
    实际入队 0 条。serenity-refresh-plan 的 summary 长期只有 {"schema": ...}，
    这条队列静默积压 7 天没被发现就是这么来的。
    """
    parsed = {
        "schema": "serenity_bus_plan_v1",
        "asof": "2026-08-07",
        "scanned": 5,
        "enqueued": 0,
        "results": [{"enqueued": False}, {"enqueued": False}],
    }

    summary = summarize_output(parsed, json.dumps(parsed))

    assert summary["schema"] == "serenity_bus_plan_v1"
    assert summary["scanned"] == 5
    assert summary["enqueued"] == 0
    assert summary["results_count"] == 2


def test_summary_exposes_auction_business_outcome_instead_of_only_transport_ok():
    parsed = {
        "schema": "auction_finalize_v2",
        "status": "ready",
        "outcome_status": "ok_research_only",
        "reason_code": "weak_market",
        "input_count": 8,
        "research_count": 8,
        "execution_count": 0,
    }

    summary = summarize_output(parsed, json.dumps(parsed))

    assert summary["status"] == "ready"
    assert summary["outcome_status"] == "ok_research_only"
    assert summary["reason_code"] == "weak_market"
    assert summary["execution_count"] == 0


def test_summary_still_reports_whitelisted_counts_and_drops_bulk_payload():
    parsed = {
        "schema": "open_confirmation_v1",
        "status": "ok",
        "signals": [{"code": "600001"}],
        "confirmations": [],
        "candidates": [{"code": "600002"}, {"code": "600003"}],
        "raw_rows": [{"blob": "x" * 5000}],
        "note": "y" * 5000,
    }

    summary = summarize_output(parsed, json.dumps(parsed))

    assert summary["signals_count"] == 1
    assert summary["confirmations_count"] == 0
    assert summary["candidates_count"] == 2
    # 大块载荷只留计数，不整体搬进 artifact（token 预算）
    assert summary["raw_rows_count"] == 1
    assert "raw_rows" not in summary
    assert "note" not in summary


def test_summary_is_bounded_for_pathological_payloads():
    parsed = {"schema": "x_v1", **{f"metric_{i}": i for i in range(200)}}

    summary = summarize_output(parsed, json.dumps(parsed))

    assert summary["schema"] == "x_v1"
    assert len(summary) <= 32


def test_empty_result_lists_outside_the_whitelist_are_not_reported_as_signal():
    parsed = {"schema": "serenity_bus_plan_v1", "scanned": 0, "results": []}

    assert output_has_signal(parsed, json.dumps(parsed)) is False


def test_runner_exports_the_manifest_timeout_to_the_job_process(tmp_path):
    """作业要能自己收口，就必须知道自己被给了多少墙钟。

    竞价采集的取数预算是从这个变量推出来的（timeout 的 80%）。之前它只存在于
    runner 进程里，子进程无从得知，于是唯一的界只有外层 SIGKILL —— 命中就同时
    丢掉已采到的行和「剩下的怎么了」。
    """
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, os\n"
        "print(json.dumps({'schema': 'demo_v1',\n"
        "                  'budget': os.environ.get('A_STOCK_JOB_TIMEOUT_SECONDS')}))\n",
        encoding="utf-8",
    )
    job = _base_job("timeout-env-demo")
    job["run"]["argv"] = [sys.executable, str(worker)]
    job["run"]["timeout_seconds"] = 180
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")

    env = os.environ.copy()
    env["A_STOCK_STATE_HOME"] = str(tmp_path / "state")
    env.pop("A_STOCK_STATE_ID", None)
    env.pop("A_STOCK_JOB_TIMEOUT_SECONDS", None)
    result = subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "scripts", "agent_job_runner.py"),
            "timeout-env-demo",
            "--manifest",
            str(manifest),
            "--runtime",
            "openclaw",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["budget"] == "180"
