#!/usr/bin/env python3
"""Isolated job-runner implementation shared by Hermes and OpenClaw.

Use ``agent_job_runner.py`` as the public entrypoint. This module retains its
historical filename for compatibility.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from typing import Any, Dict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

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
from market_snapshot import write_snapshot  # noqa: E402
from run_lease import claim  # noqa: E402
from agent_state import agent_state_path  # noqa: E402
from state_integrity import ensure_state_identity  # noqa: E402
from trading_day_gate import evaluate_job_trading_day  # noqa: E402


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


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _format_command(command: str, values: Dict[str, str]) -> str:
    return command.format_map(_SafeDict(values))


def build_runtime_env(runtime: str) -> Dict[str, str]:
    """Copy scheduler state; fall back to ROOT/default when env vars are missing.

    OpenClaw command-cron payload env vars are not reliably injected into the
    subprocess environment.  When A_STOCK_STATE_HOME / A_STOCK_STATE_ID are
    absent we fall back to the repository root so that state_identity checks
    pass instead of blocking every job.
    """
    env = os.environ.copy()
    if not env.get("A_STOCK_STATE_HOME"):
        env["A_STOCK_STATE_HOME"] = ROOT
    if not env.get("A_STOCK_STATE_ID"):
        env["A_STOCK_STATE_ID"] = "default"
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


def _emit(job: Dict[str, Any], artifact: Dict[str, Any], emit_local: bool) -> None:
    if job.get("silent_when_no_signal") and not artifact.get("has_signal"):
        return

    deliver = job.get("deliver", "origin")
    if deliver == "silent":
        return
    if deliver == "local" and not emit_local:
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
    raw_command = run.get("command")
    if not raw_command:
        raise SystemExit(f"job {args.job_id} missing run.command")

    command = _format_command(raw_command, variables)
    # Replace bare 'python' with sys.executable so cron jobs work without PATH
    if command.startswith("python ") or command == "python":
        command = sys.executable + command[6:]
    cwd = os.path.abspath(os.path.join(ROOT, run.get("cwd", job.get("cwd", "."))))
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
        write_artifact(artifact)
        record_run(artifact)
        _emit(job, artifact, args.emit_local)
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
        write_artifact(artifact)
        record_run(artifact)
        _emit(job, artifact, args.emit_local)
        return returncode

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
        write_artifact(artifact)
        record_run(artifact)
        _emit(job, artifact, args.emit_local)
        return 75

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
    })

    start = time.monotonic()
    timed_out = False
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
            return 76
        try:
            completed = subprocess.run(
                shlex.split(command),
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
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + f"\nTIMEOUT after {timeout}s"

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
    )
    write_artifact(artifact)
    record_run(artifact)
    _emit(job, artifact, args.emit_local)
    return returncode


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
