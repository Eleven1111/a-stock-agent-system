#!/usr/bin/env python3
"""Generate OpenClaw command-cron jobs from the canonical manifest.

Command payloads run the deterministic DAG directly in the Gateway scheduler.
They do not start an isolated model turn, so model cold-start time is removed
from the execution path.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from typing import Any, Mapping


def _required_order(
    jobs: Mapping[str, Mapping[str, Any]],
    target: str,
) -> list[str]:
    ordered: list[str] = []
    visited: set[str] = set()

    def visit(job_id: str) -> None:
        if job_id in visited:
            return
        if job_id not in jobs:
            raise ValueError(f"unknown cron dependency: {job_id}")
        job = jobs[job_id]
        mode = (job.get("dependency_policy") or {}).get(
            "trading_date",
            "same_trading_date",
        )
        if mode in {"same_trading_date", "same_batch"}:
            optional = set(
                (job.get("dependency_policy") or {}).get("optional_jobs") or []
            )
            for dependency in job.get("context_from") or []:
                if dependency not in optional:
                    visit(str(dependency))
        visited.add(job_id)
        ordered.append(job_id)

    visit(target)
    return ordered


def dependency_timeout_budget(
    jobs: Mapping[str, Mapping[str, Any]],
    target: str,
    *,
    grace_seconds: int = 60,
    default_attempts: int = 2,
) -> int:
    total = int(grace_seconds)
    for job_id in _required_order(jobs, target):
        job = jobs[job_id]
        timeout = int((job.get("run") or {}).get("timeout_seconds") or 120)
        attempts = max(
            1,
            int(
                (job.get("retry_policy") or {}).get(
                    "max_attempts",
                    default_attempts,
                )
            ),
        )
        total += timeout * attempts
    return total


def build_openclaw_commands(
    manifest: Mapping[str, Any],
    *,
    repo_dir: str,
    python: str,
    grace_seconds: int = 60,
    state_home: str | None = None,
    state_id: str | None = None,
) -> list[str]:
    jobs = {
        str(job["id"]): job
        for job in manifest.get("jobs", [])
        if isinstance(job, dict) and job.get("id")
    }
    commands: list[str] = []
    for job_id, job in jobs.items():
        if not job.get("enabled", True):
            continue
        argv = [
            python,
            os.path.join(repo_dir, "scripts", "run_agent_dag.py"),
            job_id,
            "--runtime",
            "openclaw",
            "--emit-target",
        ]
        timeout = dependency_timeout_budget(
            jobs,
            job_id,
            grace_seconds=grace_seconds,
        )
        output_bytes = max(4096, int(job.get("max_output_chars") or 2000) * 4)
        parts = [
            "openclaw",
            "cron",
            "create",
            str(job["schedule"]),
            "--name",
            f"A-stock: {job_id}",
            "--session",
            "isolated",
            "--command-argv",
            json.dumps(argv, ensure_ascii=False),
            "--command-cwd",
            repo_dir,
            "--timeout-seconds",
            str(timeout),
            "--output-max-bytes",
            str(output_bytes),
            "--exact",
        ]
        if state_home:
            parts.extend(["--command-env", f"A_STOCK_STATE_HOME={state_home}"])
        if state_id:
            parts.extend(["--command-env", f"A_STOCK_STATE_ID={state_id}"])
        parts.append(
            "--no-deliver"
            if job.get("deliver") in {"local", "silent"}
            else "--announce"
        )
        commands.append(" ".join(shlex.quote(value) for value in parts))
    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default="cron/hermes-cron-manifest.json",
    )
    parser.add_argument("--repo-dir", default=os.getcwd())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--grace-seconds", type=int, default=60)
    parser.add_argument("--state-home")
    parser.add_argument("--state-id")
    args = parser.parse_args()
    with open(args.manifest, encoding="utf-8") as handle:
        manifest = json.load(handle)
    for command in build_openclaw_commands(
        manifest,
        repo_dir=os.path.abspath(args.repo_dir),
        python=os.path.abspath(args.python),
        grace_seconds=args.grace_seconds,
        state_home=args.state_home,
        state_id=args.state_id,
    ):
        print(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
