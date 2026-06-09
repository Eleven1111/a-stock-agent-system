#!/usr/bin/env python3
"""
Hermes cron job runner.

The manifest should point Hermes at this runner, not directly at business
scripts. The runner executes the business command in an isolated subprocess,
writes a durable artifact, records a compact ledger entry, and emits only the
configured delivery output.
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
    load_context_from,
    make_run_id,
    now_iso,
    record_run,
    write_artifact,
)


def _load_manifest(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_job(manifest: Dict[str, Any], job_id: str) -> Dict[str, Any]:
    for job in manifest.get("jobs", []):
        if job.get("id") == job_id:
            return job
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
    cwd = os.path.abspath(os.path.join(ROOT, run.get("cwd", job.get("cwd", "."))))
    timeout = int(run.get("timeout_seconds") or job.get("timeout_seconds") or 120)
    started_at = now_iso()
    run_id = args.run_id or make_run_id(job["id"], started_at)
    context_artifacts = load_context_from(job.get("context_from", []))

    if args.dry_run:
        print(json.dumps({
            "job_id": job["id"],
            "run_id": run_id,
            "command": command,
            "cwd": cwd,
            "context_from": context_artifacts,
            "artifact_path_template": ARTIFACT_TEMPLATE,
        }, ensure_ascii=False, indent=2))
        return 0

    env = os.environ.copy()
    env.update({
        "HERMES_JOB_ID": job["id"],
        "HERMES_RUN_ID": run_id,
        "HERMES_CONTEXT_SCOPE": job.get("context_scope", "cron"),
        "HERMES_CONTEXT_FROM": json.dumps(context_artifacts, ensure_ascii=False),
    })

    start = time.monotonic()
    timed_out = False
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
    )
    write_artifact(artifact)
    record_run(artifact)
    _emit(job, artifact, args.emit_local)
    return returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one isolated Hermes cron job")
    parser.add_argument("job_id")
    parser.add_argument("--manifest", default=os.path.join(ROOT, "cron", "hermes-cron-manifest.json"))
    parser.add_argument("--run-id")
    parser.add_argument("--var", action="append", default=[], help="Template variable as key=value")
    parser.add_argument("--emit-local", action="store_true", help="Emit stdout even when deliver=local")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run_job(args))


if __name__ == "__main__":
    main()
