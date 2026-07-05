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
import subprocess
import sys
from typing import Any, Mapping, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "common"))

from cron_roles import is_scheduled  # noqa: E402


DEFAULT_DELIVERY_CHANNEL = "discord"
DEFAULT_DELIVERY_TO = "user:1068705928590917722"
DEFAULT_DELIVERY_ACCOUNT = "default"
MANAGED_JOB_PREFIX = "A-stock: "


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


def _delivery_args(job: Mapping[str, Any]) -> list[str]:
    policy = str(job.get("deliver") or "origin")
    if policy in {"local", "silent", "feishu_direct"}:
        return ["--no-deliver"]
    if policy != "origin":
        raise ValueError(f"unknown deliver policy for {job.get('id')}: {policy}")
    return [
        "--announce",
        "--channel",
        DEFAULT_DELIVERY_CHANNEL,
        "--to",
        DEFAULT_DELIVERY_TO,
        "--account",
        DEFAULT_DELIVERY_ACCOUNT,
    ]


def load_installed_openclaw_jobs(openclaw: str = "openclaw") -> list[dict[str, Any]]:
    """Read active cron definitions from OpenClaw's authoritative store."""
    completed = subprocess.run(
        [openclaw, "cron", "list", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise RuntimeError(f"openclaw cron list failed: {message}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("openclaw cron list returned invalid JSON") from exc
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
        raise RuntimeError("openclaw cron list JSON does not contain a jobs array")
    return jobs


def apply_openclaw_commands(commands: Sequence[str]) -> None:
    """Execute generated mutations without invoking a shell."""
    for command in commands:
        subprocess.run(shlex.split(command), check=True)


def build_openclaw_commands(
    manifest: Mapping[str, Any],
    *,
    repo_dir: str,
    python: str,
    grace_seconds: int = 60,
    state_home: str | None = None,
    state_id: str | None = None,
    env_file: str | None = None,
    installed_jobs: Sequence[Mapping[str, Any]] | None = None,
    openclaw: str = "openclaw",
) -> list[str]:
    state_home = state_home or os.environ.get("A_STOCK_STATE_HOME")
    state_id = state_id or os.environ.get("A_STOCK_STATE_ID")
    env_file = env_file or os.environ.get("A_STOCK_ENV_FILE")
    jobs = {
        str(job["id"]): job
        for job in manifest.get("jobs", [])
        if isinstance(job, dict) and job.get("id")
    }
    installed_by_name: dict[str, list[str]] = {}
    if installed_jobs is not None:
        for installed in installed_jobs:
            name = str(installed.get("name") or "")
            installed_id = str(installed.get("id") or installed.get("jobId") or "")
            if name and installed_id:
                installed_by_name.setdefault(name, []).append(installed_id)
    commands: list[str] = []
    for job_id, job in jobs.items():
        if not is_scheduled(job):
            continue
        name = f"{MANAGED_JOB_PREFIX}{job_id}"
        matches = installed_by_name.get(name, [])
        if len(matches) > 1:
            raise ValueError(
                f"duplicate installed OpenClaw jobs named {name}: {', '.join(matches)}"
            )
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
        if matches:
            parts = [
                openclaw,
                "cron",
                "edit",
                matches[0],
                "--name",
                name,
                "--cron",
                str(job["schedule"]),
            ]
        else:
            parts = [
                openclaw,
                "cron",
                "create",
                str(job["schedule"]),
                "--name",
                name,
            ]
        parts.extend([
            "--tz",
            str(job.get("timezone") or "Asia/Shanghai"),
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
        ])
        if state_home:
            parts.extend(["--command-env", f"A_STOCK_STATE_HOME={state_home}"])
        if state_id:
            parts.extend(["--command-env", f"A_STOCK_STATE_ID={state_id}"])
        if env_file:
            parts.extend(["--command-env", f"A_STOCK_ENV_FILE={env_file}"])
        parts.extend(_delivery_args(job))
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
    parser.add_argument("--state-home", default=os.environ.get("A_STOCK_STATE_HOME"))
    parser.add_argument("--state-id", default=os.environ.get("A_STOCK_STATE_ID"))
    parser.add_argument("--env-file", default=os.environ.get("A_STOCK_ENV_FILE"))
    parser.add_argument("--openclaw", default="openclaw")
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Edit matching installed A-stock jobs instead of creating duplicates",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply generated edits; requires --reconcile",
    )
    args = parser.parse_args()
    if not args.state_home:
        parser.error("--state-home or A_STOCK_STATE_HOME is required for OpenClaw jobs")
    if args.apply and not args.reconcile:
        parser.error("--apply requires --reconcile to avoid duplicate cron jobs")
    with open(args.manifest, encoding="utf-8") as handle:
        manifest = json.load(handle)
    installed_jobs = (
        load_installed_openclaw_jobs(args.openclaw)
        if args.reconcile
        else None
    )
    commands = build_openclaw_commands(
        manifest,
        repo_dir=os.path.abspath(args.repo_dir),
        python=os.path.abspath(args.python),
        grace_seconds=args.grace_seconds,
        state_home=args.state_home,
        state_id=args.state_id,
        env_file=args.env_file,
        installed_jobs=installed_jobs,
        openclaw=args.openclaw,
    )
    if args.apply:
        apply_openclaw_commands(commands)
    else:
        for command in commands:
            print(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
