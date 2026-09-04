"""Frozen behaviour baseline for the six representative job classes (T00).

Before the harness is changed, its externally visible contract is pinned here:
terminal status, the ``NO_REPLY`` silence decision, the dependency gate, and the
fact that a blocked or skipped run still leaves a complete audit trail.

Every case runs the real DAG, the real job runner and a real child process
against a temporary state home. Nothing here touches production state, and no
fixture carries a key, a holding, or a real external response.
"""

import json
import os
import subprocess
import sys

import pytest

import execution_trace
from scripts import run_agent_dag

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


TRADING_DAY = "2026-06-12"      # Friday
NON_TRADING_DAY = "2026-06-13"  # Saturday


def _job(job_id, argv, *, form="argv", **overrides):
    """One manifest job in either the typed argv form or the legacy shell form.

    Both forms are exercised by the same assertions: the T03 migration is only
    safe if the six representative classes behave identically either way.
    """
    if form == "argv":
        command_fields = {
            "command_argv": ["python", "scripts/run_agent_dag.py", job_id, "--emit-target"],
            "run": {"argv": [str(item) for item in argv], "cwd": ".", "timeout_seconds": 20},
            "enabled": True,
        }
    else:
        command_fields = {
            "command": f"python scripts/run_agent_dag.py {job_id} --emit-target",
            "run": {"command": " ".join(str(item) for item in argv), "cwd": ".",
                    "timeout_seconds": 20},
            # The legacy shell string is only accepted on disabled jobs.
            "enabled": False,
        }
    job = {
        "id": job_id,
        "name": job_id,
        "schedule": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
        "cwd": ".",
        "external": True,
        "expected_output": "json",
        "silent_when_no_signal": False,
        "execution_mode": "isolated_subprocess",
        "context_scope": "cron",
        "deliver": "local",
        "max_output_chars": 2000,
        "context_from": [],
        "artifact_path_template": "{cron_output_dir}/{job_id}/{run_id}.json",
        "allowed_state_writes": [],
        **command_fields,
    }
    job.update(overrides)
    return job


def _script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture(params=["argv", "legacy"], ids=["typed_argv", "legacy_shell_string"])
def harness(tmp_path, request):
    """A self-contained manifest plus an isolated state home and trace file.

    Parametrised over both command forms so every baseline assertion below is
    proven identical before and after the typed-command migration.
    """
    form = request.param
    trace_file = tmp_path / "execution_trace.jsonl"
    env = os.environ.copy()
    env["A_STOCK_STATE_HOME"] = str(tmp_path / "state")
    env["A_STOCK_EXECUTION_TRACE_PATH"] = str(trace_file)
    env.pop("A_STOCK_STATE_ID", None)
    env.pop("A_STOCK_TRACE_ID", None)

    signal = _script(
        tmp_path,
        "signal.py",
        "import json\nprint(json.dumps({'schema':'demo_v1','status':'ok','alerts':[{'code':'X'}]}))\n",
    )
    quiet = _script(
        tmp_path,
        "quiet.py",
        "import json\nprint(json.dumps({'schema':'demo_v1','status':'no_signal','alerts':[]}))\n",
    )
    fail_closed = _script(
        tmp_path,
        "fail_closed.py",
        "import json,sys\n"
        "print(json.dumps({'schema':'demo_v1','status':'insufficient_data'}))\n"
        "sys.exit(75)\n",
    )
    research = _script(
        tmp_path,
        "research.py",
        "import json\n"
        "print(json.dumps({'schema':'research_finding_v1','status':'ok',"
        "'stance':'neutral','influences_live_ranking':False}))\n",
    )

    thin = _script(
        tmp_path,
        "thin.py",
        "import json\nprint(json.dumps({'schema':'demo_v1','status':'degraded','alerts':[]}))\n",
    )

    jobs = [
        _job("silent-job", [sys.executable, str(quiet)], form=form,
             silent_when_no_signal=True, deliver="origin"),
        _job("degraded-job", [sys.executable, str(thin)], form=form),
        _job("feishu-job", [sys.executable, str(signal)], form=form,
             deliver="feishu_direct"),
        _job("upstream-job", [sys.executable, str(signal)], form=form),
        _job("dependent-job", [sys.executable, str(signal)], form=form,
             context_from=["upstream-job"],
             dependency_policy={"trading_date": "same_batch", "max_age_minutes": 60}),
        _job("calendar-job", [sys.executable, str(signal)], form=form,
             trading_day_policy="required"),
        _job("fail-closed-job", [sys.executable, str(fail_closed)], form=form),
        _job("research-only-job", [sys.executable, str(research)], form=form,
             deliver="local"),
    ]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

    def run(target, *, trading_date=TRADING_DAY, **kwargs):
        return run_agent_dag.execute_dag(
            manifest_path=str(manifest),
            targets=[target],
            trading_date=trading_date,
            runtime="openclaw",
            env=env,
            **kwargs,
        )

    def artifact(job_id, result):
        return run_agent_dag._load_artifact(
            job_id,
            trading_date=result["trading_date"],
            batch_id=result["batch_id"],
            env=env,
        )

    def job(job_id):
        return next(item for item in jobs if item["id"] == job_id)

    def events():
        return execution_trace.read_events(str(trace_file))

    return type(
        "Harness",
        (),
        {"run": staticmethod(run), "artifact": staticmethod(artifact),
         "job": staticmethod(job), "events": staticmethod(events),
         "env": env, "manifest": str(manifest), "trace_file": trace_file},
    )


def _terminal_events(events, job_id):
    return [
        event for event in events
        if event["job_id"] == job_id and event["event_type"] == "job.finished"
    ]


# --------------------------------------------------------------------------
# class 1 — silent when no signal
# --------------------------------------------------------------------------


def test_no_signal_job_stays_silent(harness):
    result = harness.run("silent-job")
    artifact = harness.artifact("silent-job", result)

    assert result["status"] == "ok"
    assert artifact["status"] == "ok"
    assert artifact["has_signal"] is False
    assert run_agent_dag.target_output(harness.job("silent-job"), artifact) == "NO_REPLY\n"


def test_degraded_payload_surfaces_without_becoming_a_failure(harness):
    """Positive control for the status propagation `no_signal` must not trigger.

    A job that exits 0 while declaring `degraded` has to carry that word up to
    the artifact and the DAG result — publishing green beside a thin-data
    summary is what this propagation exists to prevent. It must stay a *run
    that produced its target*, though: `degraded` is in the success set, so the
    batch keeps walking and the exit code stays 0.
    """
    result = harness.run("degraded-job")
    artifact = harness.artifact("degraded-job", result)

    assert artifact["status"] == "degraded"
    assert result["status"] == "degraded"
    assert result["status"] in run_agent_dag._SUCCESS_STATUSES
    assert [run["status"] for run in result["runs"]] == ["degraded"]


# --------------------------------------------------------------------------
# class 2 — direct Feishu delivery
# --------------------------------------------------------------------------


def test_feishu_direct_delivery_records_acceptance_not_receipt(harness, monkeypatch):
    calls = []

    def fake_push(job_id, text, **kwargs):
        calls.append((job_id, text))
        return {"status": "sent", "job_id": job_id}

    monkeypatch.setattr(run_agent_dag.feishu_push, "push_text", fake_push)
    monkeypatch.setenv(execution_trace.PATH_ENV, str(harness.trace_file))

    result = harness.run("feishu-job")
    artifact = harness.artifact("feishu-job", result)
    output = run_agent_dag.target_output(harness.job("feishu-job"), artifact)

    assert output == "NO_REPLY\n"
    assert calls and calls[0][0] == "feishu-job"

    types = [event["event_type"] for event in harness.events()]
    assert "delivery.attempted" in types
    assert "delivery.provider_accepted" in types
    assert "delivery.received" not in types


# --------------------------------------------------------------------------
# class 3 — same-batch dependency
# --------------------------------------------------------------------------


def test_same_batch_dependency_runs_upstream_first(harness):
    result = harness.run("dependent-job")

    assert result["status"] == "ok"
    assert [run["job_id"] for run in result["runs"]] == ["upstream-job", "dependent-job"]

    artifact = harness.artifact("dependent-job", result)
    gate = artifact["dependency_gate"]
    assert gate["passed"] is True
    assert gate["dependencies"][0]["job_id"] == "upstream-job"


def test_missing_dependency_blocks_downstream_and_stays_silent(harness):
    """The DAG resolves dependencies; the runner alone must fail closed.

    Invoked directly — as OpenClaw or a manual operator would — the dependent
    job has no upstream artifact in its batch and must block rather than run
    against missing evidence.
    """
    completed = subprocess.run(
        [
            sys.executable,
            os.path.join(REPO_ROOT, "scripts", "agent_job_runner.py"),
            "dependent-job",
            "--manifest", harness.manifest,
            "--trading-date", TRADING_DAY,
            "--batch-id", "a-share-empty-batch",
            "--runtime", "openclaw",
        ],
        cwd=REPO_ROOT,
        env=harness.env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 75, completed.stderr[-500:]

    terminal = _terminal_events(harness.events(), "dependent-job")
    assert [event["status"] for event in terminal] == ["blocked"]
    assert any(code.startswith("dep.upstream-job") for code in terminal[0]["reason_codes"])

    blocked_gates = [
        event for event in harness.events()
        if event["event_type"] == "gate.blocked" and event["job_id"] == "dependent-job"
    ]
    assert blocked_gates and blocked_gates[0]["gate"] == "dependency"


# --------------------------------------------------------------------------
# class 4 — non-trading day
# --------------------------------------------------------------------------


def test_non_trading_day_job_is_skipped_not_failed(harness):
    result = harness.run("calendar-job", trading_date=NON_TRADING_DAY)

    assert result["status"] == "skipped_non_trading_day"
    assert result["runs"][0]["returncode"] == 0

    events = harness.events()
    terminal = _terminal_events(events, "calendar-job")
    assert len(terminal) == 1
    assert terminal[0]["status"] == "skipped_non_trading_day"


# --------------------------------------------------------------------------
# class 5 — fail-closed on insufficient data
# --------------------------------------------------------------------------


def test_insufficient_data_fails_closed_as_blocked(harness):
    result = harness.run("fail-closed-job")
    artifact = harness.artifact("fail-closed-job", result)

    assert result["status"] == "blocked"
    assert artifact["status"] == "blocked"
    assert artifact["returncode"] == 75

    # The DAG retries a non-zero exit, so a fail-closed job may produce more
    # than one run. Each run must still be a separate, singly-terminated run.
    terminal = _terminal_events(harness.events(), "fail-closed-job")
    assert terminal, "no terminal trace event for a blocked job"
    assert {event["status"] for event in terminal} == {"blocked"}
    assert len({event["run_id"] for event in terminal}) == len(terminal)


# --------------------------------------------------------------------------
# class 6 — research-only agent job
# --------------------------------------------------------------------------


def test_research_only_job_never_delivers_to_the_user(harness):
    result = harness.run("research-only-job")
    artifact = harness.artifact("research-only-job", result)

    assert result["status"] == "ok"
    assert artifact["deliver"] == "local"
    assert run_agent_dag.target_output(
        harness.job("research-only-job"), artifact
    ) == "NO_REPLY\n"
    assert json.loads(artifact["stdout"])["influences_live_ranking"] is False


# --------------------------------------------------------------------------
# cross-cutting trace contract
# --------------------------------------------------------------------------


def test_every_run_has_exactly_one_terminal_event_and_no_gaps(harness):
    for target in ("silent-job", "dependent-job", "fail-closed-job", "research-only-job"):
        harness.run(target)
    harness.run("calendar-job", trading_date=NON_TRADING_DAY)

    events = harness.events()
    assert events, "shadow trace produced no events"
    assert execution_trace.find_gaps(events) == []

    runs = execution_trace.reconstruct_runs(events)
    assert all(entry["terminal_count"] == 1 for entry in runs.values())
    assert all(entry["status"] for entry in runs.values())


def test_one_dag_shares_a_trace_id_across_dependency_jobs(harness):
    harness.run("dependent-job")

    events = [event for event in harness.events() if event["event_type"] == "job.finished"]
    by_job = {event["job_id"]: event for event in events}

    assert set(by_job) == {"upstream-job", "dependent-job"}
    assert len({event["trace_id"] for event in events}) == 1
    assert len({event["run_id"] for event in events}) == 2


def test_trace_never_carries_sensitive_payload_fields(harness):
    harness.run("dependent-job")

    for event in harness.events():
        assert execution_trace.scan_sensitive_fields(event) == []
        assert "stdout" not in event and "command" not in event


def test_trace_failure_does_not_change_job_outcome(harness, tmp_path):
    blocker = tmp_path / "blocked-trace"
    blocker.write_text("not a directory", encoding="utf-8")
    env = {**harness.env, "A_STOCK_EXECUTION_TRACE_PATH": str(blocker / "trace.jsonl")}

    result = run_agent_dag.execute_dag(
        manifest_path=harness.manifest,
        targets=["silent-job"],
        trading_date=TRADING_DAY,
        runtime="openclaw",
        env=env,
    )

    assert result["status"] == "ok"


def test_trace_switch_off_leaves_business_behaviour_unchanged(harness, tmp_path):
    env = {**harness.env, execution_trace.SWITCH_ENV: "off",
           "A_STOCK_EXECUTION_TRACE_PATH": str(tmp_path / "unused.jsonl")}

    result = run_agent_dag.execute_dag(
        manifest_path=harness.manifest,
        targets=["fail-closed-job"],
        trading_date=TRADING_DAY,
        runtime="openclaw",
        env=env,
    )

    assert result["status"] == "blocked"
    assert not (tmp_path / "unused.jsonl").exists()
