#!/usr/bin/env python3
"""Isolated job-runner implementation shared by Hermes and OpenClaw.

Use ``agent_job_runner.py`` as the public entrypoint. This module retains its
historical filename for compatibility.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(ROOT)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
BLOCKED_RETURNCODES = {75, 78}

from runtime_context import (  # noqa: E402
    ARTIFACT_TEMPLATE,
    build_artifact,
    evaluate_dependencies,
    make_batch_id,
    make_run_id,
    now_iso,
    record_run,
    resolve_runtime_name,
    resolve_trading_date,
    write_artifact,
)
import execution_trace  # noqa: E402
import manifest_command  # noqa: E402
from market_snapshot import write_snapshot  # noqa: E402
from run_lease import claim  # noqa: E402
from agent_state import agent_state_path  # noqa: E402
from state_integrity import ensure_state_identity  # noqa: E402
from trading_day_gate import evaluate_job_trading_day  # noqa: E402
from a_stock_http import load_hermes_env  # noqa: E402
import feishu_push  # noqa: E402
import adaptive_schedule  # noqa: E402
import delivery_policy  # noqa: E402


def _load_manifest(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_job(manifest: Dict[str, Any], job_id: str) -> Dict[str, Any]:
    for job in manifest.get("jobs", []):
        if job.get("id") == job_id:
            return {
                "trading_day_policy": manifest.get(
                    "default_trading_day_policy",
                    "required",
                ),
                **job,
            }
    raise SystemExit(f"unknown job id: {job_id}")


def _parse_vars(items: list[str]) -> Dict[str, str]:
    result = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--var must be key=value, got: {item}")
        key, value = item.split("=", 1)
        result[key] = value
    return result


def build_runtime_env(runtime: str) -> Dict[str, str]:
    """Copy scheduler state without fabricating a state home or identity.

    Historically this injected ``A_STOCK_STATE_HOME=ROOT`` and
    ``A_STOCK_STATE_ID=default`` when those were missing.  That silently made the
    repository working tree a state root and minted a fresh identity there — the
    exact split-brain failure the identity checks now guard against.  We instead
    pass the environment through unchanged and let ``ensure_state_identity``
    resolve the real home (``HERMES_HOME`` / ``~/.hermes`` bootstrap) or fail
    closed when configuration is inconsistent.
    """
    env = load_hermes_env()
    common = os.path.join(ROOT, "skills", "common")
    pythonpath = [ROOT, common]
    existing = env.get("PYTHONPATH")
    if existing:
        pythonpath.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    return env


def _producer_version() -> str:
    configured = os.environ.get("A_STOCK_CODE_VERSION")
    if configured:
        return configured
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _push_feishu(
    job_id: str,
    text: str,
    *,
    trace_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Push to Feishu with the delivery boundary recorded on the trace."""
    ctx = dict(trace_ctx or {})
    execution_trace.delivery_attempted(channel="feishu_direct", **ctx)
    result = feishu_push.push_text(job_id, text)
    execution_trace.delivery_result(
        str(result.get("status") or "unknown"),
        channel="feishu_direct",
        **ctx,
    )
    return result


def _subprocess_text(value: Any) -> str:
    """Normalise subprocess output to text.

    ``subprocess.run(text=True, ...)`` decodes on the happy path, but the
    partial output carried by ``TimeoutExpired`` is handed back as raw bytes.
    Concatenating that with a str raised ``TypeError`` inside the timeout
    handler, so the runner died before writing anything — a job that timed out
    after emitting one stderr byte left no artifact and no ``job.finished``.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _dependency_reason_codes(dependency_gate: Optional[Dict[str, Any]]) -> List[str]:
    """Compress a dependency gate result into short, non-sensitive slugs."""
    codes: List[str] = []
    for entry in (dependency_gate or {}).get("dependencies", []):
        if entry.get("gate_status") == "passed":
            continue
        job_id = str(entry.get("job_id") or "unknown")
        for reason in entry.get("reasons") or ["unknown"]:
            codes.append(f"dep.{job_id}.{reason}")
    return codes[:20]


def _emit(
    job: Dict[str, Any],
    artifact: Dict[str, Any],
    emit_local: bool,
    trace_ctx: Optional[Dict[str, Any]] = None,
) -> None:
    if job.get("silent_when_no_signal") and not artifact.get("has_signal"):
        return

    deliver = job.get("deliver", "origin")
    if deliver == "silent":
        return
    if deliver == "local" and not emit_local:
        return
    if deliver == "feishu_direct":
        max_chars = int(job.get("max_output_chars") or 4000)
        _push_feishu(
            str(job.get("id") or artifact.get("job_id") or ""),
            feishu_push.render_artifact_text(artifact, max_chars),
            trace_ctx=trace_ctx,
        )
        return

    stdout = artifact.get("stdout", "")
    max_chars = int(job.get("max_output_chars") or 4000)
    if len(stdout) <= max_chars:
        sys.stdout.write(stdout)
        if stdout and not stdout.endswith("\n"):
            sys.stdout.write("\n")
        return

    payload = {
        "schema": "hermes_job_output_truncated_v1",
        "job_id": artifact["job_id"],
        "run_id": artifact["run_id"],
        "status": artifact["status"],
        "artifact_path": artifact.get("artifact_path"),
        "stdout_preview": stdout[:max_chars],
        "truncated_chars": len(stdout) - max_chars,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def run_job(args: argparse.Namespace) -> int:
    manifest_path = os.path.abspath(args.manifest)
    manifest = _load_manifest(manifest_path)
    job = _find_job(manifest, args.job_id)
    variables = _parse_vars(args.var or [])

    run = job.get("run") or {}
    try:
        raw_argv = manifest_command.business_argv(job)
        argv = manifest_command.substitute_argv(raw_argv, variables)
        # Bare 'python' resolves to this interpreter, so a job never depends on
        # whichever PATH the scheduler happened to inherit.
        argv = manifest_command.resolve_executable(argv, python=sys.executable)
        cwd = manifest_command.resolve_cwd(ROOT, run.get("cwd", job.get("cwd", ".")))
    except manifest_command.CommandContractError as exc:
        raise SystemExit(f"job {args.job_id} command contract error: {exc}") from exc
    command = manifest_command.display_command(argv)
    timeout = int(run.get("timeout_seconds") or job.get("timeout_seconds") or 120)
    started_at = now_iso()
    run_id = args.run_id or make_run_id(job["id"], started_at)
    calendar_date = args.calendar_date or args.trading_date or started_at[:10]
    calendar_gate = evaluate_job_trading_day(job, calendar_date)
    if calendar_gate["action"] == "block":
        trading_date = args.trading_date or calendar_date
    else:
        trading_date = resolve_trading_date(args.trading_date or calendar_date)
    batch_id = args.batch_id or make_batch_id(trading_date)
    runtime = resolve_runtime_name(args.runtime)

    run_env = build_runtime_env(runtime)

    if args.dry_run:
        dependency_gate = (
            evaluate_dependencies(
                job.get("context_from", []),
                trading_date=trading_date,
                batch_id=batch_id,
                policy=job.get("dependency_policy"),
                now=started_at,
            )
            if calendar_gate["action"] == "run"
            else None
        )
        print(json.dumps({
            "job_id": job["id"],
            "run_id": run_id,
            "batch_id": batch_id,
            "trading_date": trading_date,
            "command": command,
            "cwd": cwd,
            "calendar_gate": calendar_gate,
            "dependency_gate": dependency_gate,
            "artifact_path_template": ARTIFACT_TEMPLATE,
        }, ensure_ascii=False, indent=2))
        return 0

    # Trace is shadow-only: it observes the run, it never gates it. Every exit
    # path below therefore pairs exactly one job.finished with this start event.
    trace_ctx: Dict[str, Any] = {
        "trace_id": execution_trace.resolve_trace_id(),
        "batch_id": batch_id,
        "run_id": run_id,
        "job_id": job["id"],
        "trading_date": trading_date,
        "runtime": runtime,
    }
    execution_trace.emit("job.started", **trace_ctx)

    # Guards the one-started-one-finished contract when the crash net below
    # fires after a normal finish has already been emitted.
    finish_state = {"emitted": False}

    def _finish(
        artifact: Dict[str, Any],
        *,
        reason_codes: Optional[List[str]] = None,
        emit: bool = True,
    ) -> None:
        path = write_artifact(artifact)
        record_run(artifact)
        execution_trace.emit(
            "job.finished",
            status=str(artifact.get("status") or ""),
            artifact_ref=path,
            duration_seconds=artifact.get("duration_seconds"),
            source_versions=(artifact.get("market_snapshot") or {}).get("source_versions"),
            reason_codes=reason_codes,
            **trace_ctx,
        )
        finish_state["emitted"] = True
        if emit:
            _emit(job, artifact, args.emit_local, trace_ctx)

    state_check = ensure_state_identity(runtime, env=run_env)

    if state_check["status"] != "ok":
        artifact = build_artifact(
            job=job,
            run_id=run_id,
            command=command,
            cwd=cwd,
            returncode=78,
            stdout="",
            stderr=json.dumps(state_check, ensure_ascii=False),
            started_at=started_at,
            finished_at=now_iso(),
            duration_seconds=0,
            context_artifacts=[],
            trading_date=trading_date,
            batch_id=batch_id,
            dependency_gate=None,
            status_override="blocked_state",
            runtime=runtime,
            calendar_gate=calendar_gate,
        )
        execution_trace.emit(
            "gate.blocked",
            gate="state_identity",
            status=str(state_check.get("status") or "unknown"),
            reason_codes=[f"state_{state_check.get('status') or 'unknown'}"],
            **trace_ctx,
        )
        _finish(artifact, reason_codes=["state_identity_blocked"])
        return 78

    if calendar_gate["action"] != "run":
        status = (
            "skipped_non_trading_day"
            if calendar_gate["action"] == "skip"
            else "blocked_calendar"
        )
        returncode = 0 if calendar_gate["action"] == "skip" else 75
        artifact = build_artifact(
            job=job,
            run_id=run_id,
            command=command,
            cwd=cwd,
            returncode=returncode,
            stdout="",
            stderr=(
                ""
                if returncode == 0
                else json.dumps(calendar_gate, ensure_ascii=False)
            ),
            started_at=started_at,
            finished_at=now_iso(),
            duration_seconds=0,
            context_artifacts=[],
            trading_date=trading_date,
            batch_id=batch_id,
            dependency_gate=None,
            status_override=status,
            runtime=runtime,
            calendar_gate=calendar_gate,
        )
        if calendar_gate["action"] == "block":
            execution_trace.emit(
                "gate.blocked",
                gate="trading_calendar",
                status=status,
                reason_codes=["calendar_block"],
                **trace_ctx,
            )
        _finish(artifact, reason_codes=[f"calendar_{calendar_gate['action']}"])
        return returncode

    execution_trace.emit(
        "gate.passed", gate="trading_calendar", status="run", **trace_ctx
    )

    dependency_gate = evaluate_dependencies(
        job.get("context_from", []),
        trading_date=trading_date,
        batch_id=batch_id,
        policy=job.get("dependency_policy"),
        now=started_at,
    )
    context_artifacts = dependency_gate["dependencies"]

    if not dependency_gate["passed"]:
        finished_at = now_iso()
        artifact = build_artifact(
            job=job,
            run_id=run_id,
            command=command,
            cwd=cwd,
            returncode=75,
            stdout="",
            stderr=json.dumps(dependency_gate, ensure_ascii=False),
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=0,
            context_artifacts=context_artifacts,
            trading_date=trading_date,
            batch_id=batch_id,
            dependency_gate=dependency_gate,
            status_override="blocked",
            runtime=runtime,
            calendar_gate=calendar_gate,
        )
        dependency_codes = _dependency_reason_codes(dependency_gate)
        execution_trace.emit(
            "gate.blocked",
            gate="dependency",
            status="blocked",
            reason_codes=dependency_codes,
            **trace_ctx,
        )
        _finish(artifact, reason_codes=dependency_codes)
        return 75

    execution_trace.emit(
        "gate.passed", gate="dependency", status="passed", **trace_ctx
    )

    adaptive_decision = None
    if job.get("adaptive_backoff"):
        adaptive_decision = adaptive_schedule.should_run(job["id"])
        delivery_policy_state = delivery_policy.load_policy()
        if (
            delivery_policy.enforce(delivery_policy_state, "adaptive_backoff")
            and not adaptive_decision["run"]
        ):
            finished_at = now_iso()
            artifact = build_artifact(
                job=job,
                run_id=run_id,
                command=command,
                cwd=cwd,
                returncode=0,
                stdout="",
                stderr="",
                started_at=started_at,
                finished_at=finished_at,
                duration_seconds=0,
                context_artifacts=context_artifacts,
                trading_date=trading_date,
                batch_id=batch_id,
                dependency_gate=dependency_gate,
                status_override="skipped_adaptive_backoff",
                runtime=runtime,
                calendar_gate=calendar_gate,
                adaptive_schedule=adaptive_decision,
            )
            adaptive_schedule.record_outcome(job["id"], ran=False, has_signal=None)
            _finish(artifact, reason_codes=["adaptive_backoff"])
            return 0

    env = run_env.copy()
    env.update({
        "HERMES_JOB_ID": job["id"],
        "HERMES_RUN_ID": run_id,
        "HERMES_BATCH_ID": batch_id,
        "HERMES_TRADING_DATE": trading_date,
        "HERMES_CONTEXT_SCOPE": job.get("context_scope", "cron"),
        "HERMES_CONTEXT_FROM": json.dumps(context_artifacts, ensure_ascii=False),
        "A_STOCK_RUNTIME": runtime,
        "A_STOCK_JOB_ID": job["id"],
        "A_STOCK_RUN_ID": run_id,
        "A_STOCK_BATCH_ID": batch_id,
        "A_STOCK_TRADING_DATE": trading_date,
        "A_STOCK_AGENT_STATE_PATH": agent_state_path(),
        execution_trace.TRACE_ID_ENV: str(trace_ctx["trace_id"] or ""),
    })
    # Per-job flags declared in the manifest, so a job carries its own switches
    # instead of depending on one machine's .env. Runner-owned keys (state home,
    # run identity, PATH) are filtered out by manifest_command.env_overrides.
    env.update(manifest_command.env_overrides(run))

    start = time.monotonic()
    timed_out = False
    try:
        with claim(
            job["id"],
            trading_date=trading_date,
            batch_id=batch_id,
            run_id=run_id,
            runtime=runtime,
            ttl_seconds=max(timeout * 2, 60),
        ) as lease:
            if not lease["acquired"]:
                print(json.dumps({
                    "schema": "a_stock_duplicate_run_v1",
                    "status": "duplicate_skipped",
                    "job_id": job["id"],
                    "trading_date": trading_date,
                    "batch_id": batch_id,
                    "holder": lease.get("holder"),
                }, ensure_ascii=False))
                execution_trace.emit(
                    "job.finished",
                    status="duplicate_skipped",
                    reason_codes=["run_lease_held"],
                    **trace_ctx,
                )
                finish_state["emitted"] = True
                return 76
            try:
                completed = subprocess.run(
                    argv,
                    shell=False,
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                returncode = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                returncode = 124
                stdout = _subprocess_text(exc.stdout)
                stderr = _subprocess_text(exc.stderr) + f"\nTIMEOUT after {timeout}s"

        finished_at = now_iso()
        duration = time.monotonic() - start
        snapshot_ref = None
        parsed_output = None
        if returncode == 0:
            try:
                parsed_output = json.loads(stdout)
            except (json.JSONDecodeError, TypeError):
                parsed_output = None
        if parsed_output is not None:
            snapshot = write_snapshot(
                job["id"],
                parsed_output,
                trading_date=trading_date,
                batch_id=batch_id,
                producer=job["id"],
                producer_version=_producer_version(),
                captured_at=finished_at,
            )
            snapshot_ref = {
                key: snapshot[key]
                for key in (
                    "schema",
                    "snapshot_id",
                    "snapshot_path",
                    "payload_hash",
                    "source_versions",
                )
            }
        status_override = "blocked" if returncode in BLOCKED_RETURNCODES and not timed_out else None
        artifact = build_artifact(
            job=job,
            run_id=run_id,
            command=command,
            cwd=cwd,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            context_artifacts=context_artifacts,
            timed_out=timed_out,
            trading_date=trading_date,
            batch_id=batch_id,
            dependency_gate=dependency_gate,
            runtime=runtime,
            snapshot_ref=snapshot_ref,
            calendar_gate=calendar_gate,
            status_override=status_override,
            adaptive_schedule=adaptive_decision,
        )
        if job.get("adaptive_backoff") and artifact["status"] == "ok":
            adaptive_schedule.record_outcome(
                job["id"], ran=True, has_signal=artifact["has_signal"]
            )
        _finish(
            artifact,
            reason_codes=(["timeout"] if timed_out else None),
        )
        return returncode
    except Exception as exc:  # noqa: BLE001 - last-resort net; see comment below
        # A defect in the runner itself must not read as "this job never ran".
        # Without a terminal artifact the dependency gate, the DAG short-circuit
        # and any watchdog all see missing data and keep respawning the job.
        detail = traceback.format_exc()
        sys.stderr.write(detail)
        if finish_state["emitted"]:
            return 1
        summary = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        crash_artifact = build_artifact(
            job=job,
            run_id=run_id,
            command=command,
            cwd=cwd,
            returncode=1,
            stdout="",
            stderr=f"RUNNER CRASH: {summary}\n{detail[-2000:]}",
            started_at=started_at,
            finished_at=now_iso(),
            duration_seconds=time.monotonic() - start,
            context_artifacts=context_artifacts,
            trading_date=trading_date,
            batch_id=batch_id,
            dependency_gate=dependency_gate,
            status_override="failed",
            runtime=runtime,
            calendar_gate=calendar_gate,
        )
        try:
            # Nothing to deliver on a crash: emit=False keeps a broken run out
            # of the push channels while still landing the artifact.
            _finish(crash_artifact, reason_codes=["runner_crash"], emit=False)
        except (OSError, TimeoutError):
            # Artifact storage is unreachable; the trace contract outlives it.
            execution_trace.emit(
                "job.finished",
                status="failed",
                reason_codes=["runner_crash", "artifact_write_failed"],
                **trace_ctx,
            )
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one isolated A-stock agent job")
    parser.add_argument("job_id")
    parser.add_argument("--manifest", default=os.path.join(ROOT, "cron", "hermes-cron-manifest.json"))
    parser.add_argument("--run-id")
    parser.add_argument("--batch-id")
    parser.add_argument("--trading-date")
    parser.add_argument("--calendar-date")
    parser.add_argument("--runtime", choices=["hermes", "openclaw", "local"])
    parser.add_argument("--var", action="append", default=[], help="Template variable as key=value")
    parser.add_argument("--emit-local", action="store_true", help="Emit stdout even when deliver=local")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(run_job(args))


if __name__ == "__main__":
    main()
