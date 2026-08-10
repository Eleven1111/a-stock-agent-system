import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from scripts import run_agent_dag


def test_direct_entrypoint_bootstraps_repo_root_before_skills_import():
    source = Path(run_agent_dag.__file__).read_text(encoding="utf-8")
    root_setup = source.index("sys.path.insert(0, ROOT)")
    skills_import = source.index("import skills.common")

    assert root_setup < skills_import


def test_job_runner_entrypoint_bootstraps_repo_root_before_skills_import():
    source = (Path(run_agent_dag.__file__).parent / "hermes_job_runner.py").read_text(
        encoding="utf-8"
    )
    root_setup = source.index("sys.path.insert(0, ROOT)")
    skills_import = source.index("import skills.common")

    assert root_setup < skills_import


def test_job_runner_runtime_env_includes_repo_and_common_paths():
    from scripts import hermes_job_runner

    env = hermes_job_runner.build_runtime_env("hermes")
    paths = env["PYTHONPATH"].split(os.pathsep)

    assert str(Path(hermes_job_runner.ROOT)) in paths
    assert str(Path(hermes_job_runner.ROOT) / "skills" / "common") in paths


def _job(job_id, command, dependencies=None, mode="same_trading_date"):
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
        "deliver": "local",
        "max_output_chars": 2000,
        "context_from": dependencies or [],
        "dependency_policy": {"trading_date": mode, "max_age_minutes": 60},
        "artifact_path_template": "{cron_output_dir}/{job_id}/{run_id}.json",
        "allowed_state_writes": [],
        "run": {"argv": shlex.split(command), "cwd": ".", "timeout_seconds": 10},
    }


def test_dag_reuses_dependencies_but_reruns_scheduled_target(tmp_path):
    log = tmp_path / "order.log"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json,sys\n"
        "from pathlib import Path\n"
        "p=Path(sys.argv[1]); p.write_text((p.read_text() if p.exists() else '')+sys.argv[2]+'\\n')\n"
        "print(json.dumps({'schema':'demo_v1','job':sys.argv[2]}))\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    jobs = [
        _job("upstream", f"{sys.executable} {worker} {log} upstream"),
        _job("downstream", f"{sys.executable} {worker} {log} downstream", ["upstream"]),
    ]
    manifest.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    env = os.environ.copy()
    env["A_STOCK_STATE_HOME"] = str(tmp_path / "state")
    env.pop("A_STOCK_STATE_ID", None)

    first = run_agent_dag.execute_dag(
        manifest_path=str(manifest),
        targets=["downstream"],
        trading_date="2026-06-12",
        runtime="openclaw",
        env=env,
    )
    second = run_agent_dag.execute_dag(
        manifest_path=str(manifest),
        targets=["downstream"],
        trading_date="2026-06-12",
        runtime="hermes",
        env=env,
    )

    assert first["status"] == "ok"
    assert [item["job_id"] for item in first["runs"]] == ["upstream", "downstream"]
    assert log.read_text(encoding="utf-8").splitlines() == ["upstream", "downstream", "downstream"]
    assert second["status"] == "ok"
    assert second["runs"][0]["status"] == "reused"
    assert second["runs"][1]["status"] == "ok"


def test_dag_can_explicitly_resume_target(tmp_path):
    log = tmp_path / "order.log"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json,sys\n"
        "from pathlib import Path\n"
        "p=Path(sys.argv[1]); p.write_text((p.read_text() if p.exists() else '')+sys.argv[2]+'\\n')\n"
        "print(json.dumps({'schema':'demo_v1','job':sys.argv[2]}))\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "jobs": [_job("target", f"{sys.executable} {worker} {log} target")]
    }), encoding="utf-8")
    env = os.environ.copy()
    env["A_STOCK_STATE_HOME"] = str(tmp_path / "state")
    env.pop("A_STOCK_STATE_ID", None)

    run_agent_dag.execute_dag(
        manifest_path=str(manifest),
        targets=["target"],
        trading_date="2026-06-12",
        env=env,
    )
    resumed = run_agent_dag.execute_dag(
        manifest_path=str(manifest),
        targets=["target"],
        trading_date="2026-06-12",
        env=env,
        reuse_targets=True,
    )

    assert resumed["runs"][0]["status"] == "reused"
    assert log.read_text(encoding="utf-8").splitlines() == ["target"]


def test_previous_trading_day_dependency_is_not_rerun_in_current_batch():
    jobs = {
        "candidate": _job("candidate", "true"),
        "auction": _job(
            "auction",
            "true",
            ["candidate"],
            mode="previous_trading_day",
        ),
    }

    assert run_agent_dag.execution_order(jobs, ["auction"]) == ["auction"]


def test_tail_close_manifest_has_no_future_data_dependency():
    with open(
        "cron/hermes-cron-manifest.json",
        encoding="utf-8",
    ) as handle:
        jobs = {
            job["id"]: job
            for job in json.load(handle)["jobs"]
        }

    assert run_agent_dag.execution_order(
        jobs,
        ["tail-close-decision"],
    ) == ["tail-close-prepare", "tail-close-decision"]
    dependencies = set(jobs["tail-close-decision"]["context_from"])
    assert dependencies.isdisjoint(
        {"candidate-discovery", "candidate-freshness-check", "snapshot-gc"}
    )
    assert run_agent_dag.execution_order(
        jobs,
        ["tail-close-after-hours-shadow"],
    ) == ["tail-close-after-hours-shadow"]
    assert run_agent_dag.execution_order(
        jobs,
        ["tail-close-after-hours-reconcile"],
    ) == [
        "tail-close-after-hours-shadow",
        "tail-close-after-hours-reconcile",
    ]


def test_wait_for_concurrent_holder_uses_exact_run_id(monkeypatch):
    artifacts = iter([
        {"run_id": "older-run", "status": "ok"},
        {"run_id": "holder-run", "status": "ok", "artifact_path": "/tmp/holder.json"},
    ])
    monkeypatch.setattr(
        run_agent_dag,
        "_load_artifact",
        lambda *args, **kwargs: next(artifacts),
    )
    monkeypatch.setattr(run_agent_dag.time, "sleep", lambda _seconds: None)

    artifact = run_agent_dag._wait_for_run_artifact(
        "open-confirmation",
        "holder-run",
        trading_date="2026-06-12",
        batch_id="a-share-20260612",
        env={},
        timeout_seconds=1,
    )

    assert artifact["artifact_path"] == "/tmp/holder.json"


def test_dag_does_not_rerun_after_concurrent_holder_times_out(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "jobs": [_job("capital-flow", "true")]
    }), encoding="utf-8")
    terminal = {
        "run_id": "holder-run",
        "status": "timeout",
        "returncode": 124,
        "artifact_path": "/tmp/holder.json",
    }
    calls = []

    monkeypatch.setattr(
        run_agent_dag,
        "evaluate_job_trading_day",
        lambda _job, _date: {"action": "run"},
    )
    monkeypatch.setattr(run_agent_dag, "_load_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_agent_dag, "_wait_for_run_artifact", lambda *args, **kwargs: terminal)
    monkeypatch.setattr(
        run_agent_dag.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(args) or subprocess.CompletedProcess(
            args[0], 76,
            stdout=json.dumps({"holder": {"run_id": "holder-run"}}),
            stderr="",
        ),
    )

    result = run_agent_dag.execute_dag(
        manifest_path=str(manifest),
        targets=["capital-flow"],
        trading_date="2026-06-12",
        batch_id="a-share-20260612",
        env={"A_STOCK_STATE_HOME": str(tmp_path / "state")},
    )

    assert len(calls) == 1
    assert result["status"] == "failed"
    assert result["runs"][0]["artifact_path"] == "/tmp/holder.json"


def _seed_artifact(state_home, job_id, *, run_id, status, trading_date, batch_id):
    directory = state_home / "cron" / "output" / job_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.json"
    path.write_text(json.dumps({
        "schema": "hermes_cron_artifact_v2",
        "job_id": job_id,
        "run_id": run_id,
        "batch_id": batch_id,
        "trading_date": trading_date,
        "status": status,
        "returncode": 124 if status == "timeout" else 1,
        "artifact_path": str(path),
        "started_at": f"{trading_date}T09:15:00+08:00",
        "finished_at": f"{trading_date}T09:25:00+08:00",
        "stdout": "",
        "stderr": "TIMEOUT after 600s",
    }), encoding="utf-8")
    return path


def test_dag_blocks_instead_of_rerunning_a_dependency_that_already_failed(
    tmp_path,
    monkeypatch,
):
    """A dependency that already failed today must not be respawned.

    Issue #159: every downstream firing re-launched the same expensive upstream
    job, multiplying one failure by (firings x attempts x lease waits).
    """
    state_home = tmp_path / "state"
    day = "2026-06-12"
    batch = run_agent_dag.make_batch_id(day)
    _seed_artifact(
        state_home,
        "upstream",
        run_id="upstream-timeout",
        status="timeout",
        trading_date=day,
        batch_id=batch,
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [
        _job("upstream", "true"),
        _job("downstream", "true", ["upstream"]),
    ]}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        run_agent_dag.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(args),
    )
    env = os.environ.copy()
    env["A_STOCK_STATE_HOME"] = str(state_home)
    env.pop("A_STOCK_STATE_ID", None)

    result = run_agent_dag.execute_dag(
        manifest_path=str(manifest),
        targets=["downstream"],
        trading_date=day,
        batch_id=batch,
        env=env,
    )

    assert calls == []
    assert result["status"] == "blocked"
    assert result["runs"] == [{
        "job_id": "upstream",
        "status": "blocked",
        "reason": "upstream_failed",
        "upstream_status": "timeout",
        "upstream_run_id": "upstream-timeout",
        "artifact_path": str(
            state_home / "cron" / "output" / "upstream" / "upstream-timeout.json"
        ),
    }]


def test_dag_still_bootstraps_a_dependency_that_never_ran(tmp_path, monkeypatch):
    """Missing != failed. A machine that slept through cron must still bootstrap."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [
        _job("upstream", "true"),
        _job("downstream", "true", ["upstream"]),
    ]}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        run_agent_dag.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(args[0][2]) or subprocess.CompletedProcess(
            args[0], 0, stdout="", stderr=""
        ),
    )
    env = os.environ.copy()
    env["A_STOCK_STATE_HOME"] = str(tmp_path / "state")
    env.pop("A_STOCK_STATE_ID", None)

    result = run_agent_dag.execute_dag(
        manifest_path=str(manifest),
        targets=["downstream"],
        trading_date="2026-06-12",
        env=env,
    )

    assert calls == ["upstream", "downstream"]
    assert result["status"] == "ok"


def test_dag_never_short_circuits_the_target_itself(tmp_path, monkeypatch):
    """Rerunning a failed target by hand stays possible."""
    state_home = tmp_path / "state"
    day = "2026-06-12"
    batch = run_agent_dag.make_batch_id(day)
    _seed_artifact(
        state_home,
        "target",
        run_id="target-failed",
        status="failed",
        trading_date=day,
        batch_id=batch,
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"jobs": [_job("target", "true")]}), encoding="utf-8"
    )
    calls = []
    monkeypatch.setattr(
        run_agent_dag.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(args[0][2]) or subprocess.CompletedProcess(
            args[0], 0, stdout="", stderr=""
        ),
    )
    env = os.environ.copy()
    env["A_STOCK_STATE_HOME"] = str(state_home)
    env.pop("A_STOCK_STATE_ID", None)

    result = run_agent_dag.execute_dag(
        manifest_path=str(manifest),
        targets=["target"],
        trading_date=day,
        batch_id=batch,
        env=env,
    )

    assert calls == ["target"]
    assert result["status"] == "ok"


def test_blocked_dag_exits_nonzero_and_explains_itself(monkeypatch, capsys):
    """blocked used to exit 0 with NO_REPLY — a silent failure by construction."""
    monkeypatch.setattr(
        run_agent_dag,
        "execute_dag",
        lambda **kwargs: {
            "schema": "a_stock_dag_run_v1",
            "status": "blocked",
            "runtime": "hermes",
            "trading_date": "2026-06-12",
            "batch_id": "a-share-20260612",
            "targets": ["auction-snapshot"],
            "runs": [{
                "job_id": "candidate-preopen",
                "status": "blocked",
                "reason": "upstream_failed",
                "upstream_status": "timeout",
            }],
        },
    )
    monkeypatch.setattr(
        run_agent_dag.sys,
        "argv",
        ["run_agent_dag.py", "auction-snapshot", "--emit-target"],
    )

    try:
        run_agent_dag.main()
    except SystemExit as exc:
        code = exc.code

    output = capsys.readouterr().out
    assert code == 1
    assert "NO_REPLY" not in output
    assert "auction-snapshot" in output
    assert "candidate-preopen" in output


def test_skipped_non_trading_day_stays_silent_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        run_agent_dag,
        "execute_dag",
        lambda **kwargs: {
            "schema": "a_stock_dag_run_v1",
            "status": "skipped_non_trading_day",
            "runtime": "hermes",
            "trading_date": "2026-06-13",
            "batch_id": "a-share-20260613",
            "targets": ["auction-snapshot"],
            "runs": [],
        },
    )
    monkeypatch.setattr(
        run_agent_dag.sys,
        "argv",
        ["run_agent_dag.py", "auction-snapshot", "--emit-target"],
    )

    try:
        run_agent_dag.main()
    except SystemExit as exc:
        code = exc.code

    assert code == 0
    assert capsys.readouterr().out.strip() == "NO_REPLY"


def test_target_output_records_push_telemetry_jsonl(tmp_path):
    telemetry = tmp_path / "state" / "cron" / "push_telemetry.jsonl"

    delivered = run_agent_dag.target_output(
        {
            "id": "signal-digest",
            "deliver": "origin",
            "silent_when_no_signal": False,
            "max_output_chars": 20,
        },
        {
            "trading_date": "2026-06-12",
            "stdout": "x" * 500,
            "has_signal": True,
            "summary": {"message": "digest ok", "signals_count": 3},
        },
        telemetry_path=str(telemetry),
        record_telemetry=True,
    )
    silent = run_agent_dag.target_output(
        {
            "id": "quiet-monitor",
            "deliver": "origin",
            "silent_when_no_signal": True,
        },
        {
            "trading_date": "2026-06-12",
            "stdout": '{"status":"no_signal","signals":[]}',
            "has_signal": False,
        },
        telemetry_path=str(telemetry),
        record_telemetry=True,
    )

    records = [
        json.loads(line)
        for line in telemetry.read_text(encoding="utf-8").splitlines()
    ]

    assert "已压缩" in delivered
    assert silent == "NO_REPLY\n"
    assert records == [
        {
            "job_id": "signal-digest",
            "trading_date": "2026-06-12",
            "delivered": True,
            "output_chars": len(delivered),
            "was_compressed": True,
            "silent_reason": "none",
        },
        {
            "job_id": "quiet-monitor",
            "trading_date": "2026-06-12",
            "delivered": False,
            "output_chars": 0,
            "was_compressed": False,
            "silent_reason": "no_signal",
        },
    ]


def test_target_output_pushes_feishu_direct_jobs_without_entering_reply(tmp_path, monkeypatch):
    telemetry = tmp_path / "state" / "cron" / "push_telemetry.jsonl"
    calls = []
    monkeypatch.setattr(
        run_agent_dag.feishu_push,
        "push_text",
        lambda job_id, text: calls.append((job_id, text)) or {"status": "sent", "job_id": job_id},
    )

    output = run_agent_dag.target_output(
        {
            "id": "capital-flow",
            "deliver": "feishu_direct",
            "silent_when_no_signal": False,
            "max_output_chars": 20,
        },
        {
            "trading_date": "2026-06-12",
            "stdout": "北向资金净流入 12.3 亿" * 5,
            "has_signal": True,
        },
        telemetry_path=str(telemetry),
        record_telemetry=True,
    )

    assert output == "NO_REPLY\n"
    assert calls[0][0] == "capital-flow"
    record = json.loads(telemetry.read_text(encoding="utf-8").splitlines()[0])
    assert record["delivered"] is True
    assert record["silent_reason"] == "none"


def test_target_output_records_not_configured_feishu_push(tmp_path, monkeypatch):
    telemetry = tmp_path / "state" / "cron" / "push_telemetry.jsonl"
    monkeypatch.setattr(
        run_agent_dag.feishu_push,
        "push_text",
        lambda job_id, text: {"status": "not_configured", "job_id": job_id},
    )

    output = run_agent_dag.target_output(
        {
            "id": "event-calendar",
            "deliver": "feishu_direct",
            "silent_when_no_signal": False,
            "max_output_chars": 200,
        },
        {"trading_date": "2026-06-12", "stdout": "本周事件日历", "has_signal": True},
        telemetry_path=str(telemetry),
        record_telemetry=True,
    )

    # 飞书未配置时不再静默吞掉消息：回落 OpenClaw 通道正常投递，
    # 遥测保留两跳记录（飞书未配置 + 回落投递成功）
    assert output == "本周事件日历\n"
    records = [
        json.loads(line)
        for line in telemetry.read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["delivered"] is False
    assert records[0]["silent_reason"] == "feishu_not_configured"
    assert records[1]["delivered"] is True
    assert records[1]["silent_reason"] == "none"
