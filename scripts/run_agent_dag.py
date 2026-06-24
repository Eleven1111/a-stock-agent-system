#!/usr/bin/env python3
"""Run manifest jobs as a resumable dependency DAG for Hermes or OpenClaw."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

from runtime_context import (  # noqa: E402
    load_latest_artifact,
    make_batch_id,
    resolve_runtime_name,
    resolve_trading_date,
)
from trading_day_gate import evaluate_job_trading_day  # noqa: E402


def _load_manifest(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _load_artifact(
    job_id: str,
    *,
    trading_date: str,
    batch_id: str,
    env: Mapping[str, str],
) -> dict[str, Any] | None:
    keys = ("A_STOCK_STATE_HOME", "HERMES_HOME")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            value = env.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return load_latest_artifact(
            job_id,
            trading_date=trading_date,
            batch_id=batch_id,
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def execution_order(
    jobs: Mapping[str, Mapping[str, Any]],
    targets: list[str],
) -> list[str]:
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(job_id: str) -> None:
        if job_id in visited:
            return
        if job_id in visiting:
            raise ValueError(f"same-batch dependency cycle at {job_id}")
        if job_id not in jobs:
            raise ValueError(f"unknown DAG job: {job_id}")
        visiting.add(job_id)
        job = jobs[job_id]
        mode = (job.get("dependency_policy") or {}).get("trading_date", "same_trading_date")
        if mode in {"same_trading_date", "same_batch"}:
            optional = set((job.get("dependency_policy") or {}).get("optional_jobs") or [])
            for dependency in job.get("context_from") or []:
                if dependency not in optional:
                    visit(str(dependency))
        visiting.remove(job_id)
        visited.add(job_id)
        ordered.append(job_id)

    for target in targets:
        visit(target)
    return ordered


def _wait_for_run_artifact(
    job_id: str,
    run_id: str,
    *,
    trading_date: str,
    batch_id: str,
    env: Mapping[str, str],
    timeout_seconds: float,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        artifact = _load_artifact(
            job_id,
            trading_date=trading_date,
            batch_id=batch_id,
            env=env,
        )
        if artifact and artifact.get("run_id") == run_id:
            return artifact
        time.sleep(0.1)
    return None


def execute_dag(
    *,
    manifest_path: str,
    targets: list[str],
    trading_date: Optional[str] = None,
    batch_id: Optional[str] = None,
    runtime: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    max_attempts: int = 2,
    reuse_targets: bool = False,
) -> dict[str, Any]:
    manifest_path = os.path.abspath(manifest_path)
    manifest = _load_manifest(manifest_path)
    default_day_policy = manifest.get("default_trading_day_policy", "required")
    jobs = {
        job["id"]: {"trading_day_policy": default_day_policy, **job}
        for job in manifest.get("jobs", [])
    }
    run_env = dict(env or os.environ)
    runtime = resolve_runtime_name(runtime, run_env)
    run_env["A_STOCK_RUNTIME"] = runtime
    calendar_date = (
        str(trading_date)[:10]
        if trading_date
        else datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    )
    active_targets: list[str] = []
    runs: list[dict[str, Any]] = []
    for target in targets:
        if target not in jobs:
            raise ValueError(f"unknown DAG job: {target}")
        gate = evaluate_job_trading_day(jobs[target], calendar_date)
        if gate["action"] == "run":
            active_targets.append(target)
            continue
        command = [
            sys.executable,
            os.path.join(ROOT, "scripts", "agent_job_runner.py"),
            target,
            "--manifest",
            manifest_path,
            "--calendar-date",
            calendar_date,
            "--runtime",
            runtime,
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=run_env,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 78:
            run_status = "blocked_state"
        elif completed.returncode == 75:
            run_status = "blocked_calendar"
        else:
            run_status = (
                "skipped_non_trading_day"
                if gate["action"] == "skip"
                else "blocked_calendar"
            )
        runs.append({
            "job_id": target,
            "status": run_status,
            "returncode": completed.returncode,
            "stderr": completed.stderr[-1000:],
        })

    if not active_targets:
        blocked = any(run["returncode"] != 0 for run in runs)
        result_day = (
            str(trading_date or calendar_date)[:10]
            if blocked
            else resolve_trading_date(trading_date or calendar_date)
        )
        return {
            "schema": "a_stock_dag_run_v1",
            "status": "blocked" if blocked else "skipped_non_trading_day",
            "runtime": runtime,
            "trading_date": result_day,
            "batch_id": batch_id or make_batch_id(result_day),
            "targets": targets,
            "runs": runs,
        }

    day = resolve_trading_date(trading_date or calendar_date)
    batch = batch_id or make_batch_id(day)
    order = execution_order(jobs, active_targets)
    target_set = set(active_targets)

    for job_id in order:
        existing = _load_artifact(
            job_id,
            trading_date=day,
            batch_id=batch,
            env=run_env,
        )
        can_reuse = job_id not in target_set or reuse_targets
        if can_reuse and existing and existing.get("status") == "ok":
            runs.append({
                "job_id": job_id,
                "status": "reused",
                "artifact_path": existing.get("artifact_path"),
                "run_id": existing.get("run_id"),
            })
            continue

        command = [
            sys.executable,
            os.path.join(ROOT, "scripts", "agent_job_runner.py"),
            job_id,
            "--manifest",
            manifest_path,
            "--trading-date",
            day,
            "--calendar-date",
            calendar_date,
            "--batch-id",
            batch,
            "--runtime",
            runtime,
        ]
        completed = None
        concurrent_artifact = None
        attempts = max(1, int((jobs[job_id].get("retry_policy") or {}).get("max_attempts", max_attempts)))
        for attempt in range(1, attempts + 1):
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=run_env,
                capture_output=True,
                text=True,
            )
            if completed.returncode == 0:
                break
            if completed.returncode == 76:
                try:
                    duplicate = json.loads(completed.stdout)
                except json.JSONDecodeError:
                    duplicate = {}
                holder_run_id = str((duplicate.get("holder") or {}).get("run_id") or "")
                if holder_run_id:
                    timeout_seconds = float(
                        (jobs[job_id].get("run") or {}).get("timeout_seconds") or 120
                    )
                    concurrent_artifact = _wait_for_run_artifact(
                        job_id,
                        holder_run_id,
                        trading_date=day,
                        batch_id=batch,
                        env=run_env,
                        timeout_seconds=timeout_seconds,
                    )
                    if concurrent_artifact and concurrent_artifact.get("status") == "ok":
                        break
        artifact = _load_artifact(
            job_id,
            trading_date=day,
            batch_id=batch,
            env=run_env,
        )
        reused_concurrent = bool(
            concurrent_artifact and concurrent_artifact.get("status") == "ok"
        )
        status = "ok" if completed and (completed.returncode == 0 or reused_concurrent) else "failed"
        runs.append({
            "job_id": job_id,
            "status": "reused_concurrent" if reused_concurrent else status,
            "attempts": attempt,
            "returncode": completed.returncode if completed else 1,
            "artifact_path": (concurrent_artifact or artifact or {}).get("artifact_path"),
            "stderr": (completed.stderr if completed else "")[-1000:],
        })
        if status != "ok":
            return {
                "schema": "a_stock_dag_run_v1",
                "status": "failed",
                "runtime": runtime,
                "trading_date": day,
                "batch_id": batch,
                "targets": targets,
                "runs": runs,
            }

    return {
        "schema": "a_stock_dag_run_v1",
        "status": "ok",
        "runtime": runtime,
        "trading_date": day,
        "batch_id": batch,
        "targets": targets,
        "runs": runs,
    }


def target_output(job: Mapping[str, Any], artifact: Mapping[str, Any]) -> str:
    """Return bounded target output without bypassing delivery/no-signal policy."""
    if job.get("deliver") in {"local", "silent"}:
        return "NO_REPLY\n"
    if job.get("silent_when_no_signal") and not artifact.get("has_signal"):
        return "NO_REPLY\n"
    stdout = str(artifact.get("stdout") or "")
    max_chars = max(200, int(job.get("max_output_chars") or 4000))
    if len(stdout) > max_chars:
        raw_summary = artifact.get("summary")
        summary = raw_summary if isinstance(raw_summary, Mapping) else {}
        parts = [summary.get("message", f"{job.get('id', 'job')} 运行完成")]
        status = summary.get("status")
        if status and status != "ok":
            parts.append(f"状态={status}")
        count_keys = {k: v for k, v in summary.items() if k.endswith("_count") and v is not None}
        if count_keys:
            parts.append(" | ".join(f"{k.replace('_count','')}={v}" for k, v in count_keys.items()))
        parts.append(f"(输出{len(stdout)}字符，已压缩)")
        stdout = "\n".join(parts)[:max_chars]
    return stdout + ("\n" if stdout and not stdout.endswith("\n") else "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="+")
    parser.add_argument("--manifest", default=os.path.join(ROOT, "cron", "hermes-cron-manifest.json"))
    parser.add_argument("--trading-date")
    parser.add_argument("--batch-id")
    parser.add_argument("--runtime", choices=["hermes", "openclaw", "local"])
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--reuse-targets", action="store_true")
    parser.add_argument(
        "--emit-target",
        action="store_true",
        help="Emit the single target job stdout instead of the DAG summary",
    )
    args = parser.parse_args()
    result = execute_dag(
        manifest_path=args.manifest,
        targets=args.targets,
        trading_date=args.trading_date,
        batch_id=args.batch_id,
        runtime=args.runtime,
        max_attempts=args.max_attempts,
        reuse_targets=args.reuse_targets,
    )
    if args.emit_target and result["status"] == "skipped_non_trading_day":
        print("NO_REPLY")
    elif args.emit_target and result["status"] == "ok" and len(args.targets) == 1:
        artifact = _load_artifact(
            args.targets[0],
            trading_date=result["trading_date"],
            batch_id=result["batch_id"],
            env=os.environ,
        )
        manifest = _load_manifest(args.manifest)
        job = next(
            (item for item in manifest.get("jobs", []) if item.get("id") == args.targets[0]),
            {},
        )
        sys.stdout.write(target_output(job, artifact or {}))
    else:
        # 任务失败时输出人类可读的短消息而不是原始 DAG JSON
        failed_runs = result.get("runs", [])
        failed_jobs = [f"{r['job_id']}(code={r['returncode']})" for r in failed_runs if r.get("status") == "failed"]
        parts = [f"任务失败: {', '.join(failed_jobs)}"] if failed_jobs else [f"DAG运行失败: {result.get('status')}"]
        artifact = (failed_runs or [{}])[0]
        if artifact.get("stderr"):
            err_msg = artifact["stderr"][:200]
            parts.append(f"错误: {err_msg}")
        print("\n".join(parts))
    raise SystemExit(
        0 if result["status"] in {"ok", "skipped_non_trading_day"} else 1
    )


if __name__ == "__main__":
    main()
