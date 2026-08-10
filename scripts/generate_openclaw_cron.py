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


DEFAULT_DELIVERY_CHANNEL = "discord"
DEFAULT_DELIVERY_ACCOUNT = "default"
MANAGED_JOB_PREFIX = "A-stock: "


def default_python(repo_dir: str) -> str:
    """Prefer the repository interpreter used by the dispatcher.

    Scheduler processes do not inherit the shell's virtualenv activation, so
    falling back to the caller's interpreter silently creates mixed-runtime
    cron jobs.  Keep a fallback for repositories that deliberately have no
    local venv.
    """
    candidate = os.path.join(os.path.abspath(repo_dir), ".venv", "bin", "python")
    return candidate if os.path.isfile(candidate) else sys.executable


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


def _delivery_args(
    job: Mapping[str, Any],
    *,
    channel: str,
    target: str | None,
    account: str,
) -> list[str]:
    policy = str(job.get("deliver") or "origin")
    if policy in {"local", "silent", "feishu_direct"}:
        return ["--no-deliver"]
    if policy != "origin":
        raise ValueError(f"unknown deliver policy for {job.get('id')}: {policy}")
    if not target:
        raise ValueError(
            "origin delivery target is required; set --delivery-to or "
            "A_STOCK_DELIVERY_TO"
        )
    return [
        "--announce",
        "--channel",
        channel,
        "--to",
        target,
        "--account",
        account,
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


def validate_installed_job_uniqueness(
    installed_jobs: Sequence[Mapping[str, Any]],
    *,
    managed_prefix: str = MANAGED_JOB_PREFIX,
) -> None:
    """Reject duplicate managed names before any reconciliation is generated."""
    seen: dict[str, list[str]] = {}
    for installed in installed_jobs:
        name = str(installed.get("name") or "")
        if not name.startswith(managed_prefix):
            continue
        installed_id = str(installed.get("id") or installed.get("jobId") or "")
        if name and installed_id:
            seen.setdefault(name, []).append(installed_id)
    duplicates = {
        name: ids for name, ids in seen.items() if len(ids) > 1
    }
    if duplicates:
        detail = "; ".join(
            f"{name}: {', '.join(ids)}" for name, ids in sorted(duplicates.items())
        )
        raise ValueError(f"duplicate installed OpenClaw jobs: {detail}")


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
    delivery_channel: str | None = None,
    delivery_to: str | None = None,
    delivery_account: str | None = None,
) -> list[str]:
    state_home = state_home or os.environ.get("A_STOCK_STATE_HOME")
    state_id = state_id or os.environ.get("A_STOCK_STATE_ID")
    env_file = env_file or os.environ.get("A_STOCK_ENV_FILE")
    delivery_channel = (
        delivery_channel
        or os.environ.get("A_STOCK_DELIVERY_CHANNEL")
        or DEFAULT_DELIVERY_CHANNEL
    )
    delivery_to = delivery_to or os.environ.get("A_STOCK_DELIVERY_TO")
    delivery_account = (
        delivery_account
        or os.environ.get("A_STOCK_DELIVERY_ACCOUNT")
        or DEFAULT_DELIVERY_ACCOUNT
    )
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
        if not job.get("enabled", True):
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
        # Backup home (used by provider-health, ledger-projector, hk-a-linkage, market-pulse-*)
        backup_home = os.environ.get("A_STOCK_BACKUP_HOME", "")
        if backup_home:
            parts.extend(["--command-env", f"A_STOCK_BACKUP_HOME={backup_home}"])
        # Secrets and recipient identities are loaded by the child runner from
        # A_STOCK_ENV_FILE. Never serialize their values into cron commands,
        # dry-run output, process arguments, or the OpenClaw job store.
        parts.extend(_delivery_args(
            job,
            channel=delivery_channel,
            target=delivery_to,
            account=delivery_account,
        ))
        commands.append(" ".join(shlex.quote(value) for value in parts))
    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default="cron/hermes-cron-manifest.json",
    )
    parser.add_argument("--repo-dir", default=os.getcwd())
    parser.add_argument(
        "--python",
        default=None,
        help="Python executable (default: <repo-dir>/.venv/bin/python when present)",
    )
    parser.add_argument("--grace-seconds", type=int, default=60)
    parser.add_argument("--state-home", default=os.environ.get("A_STOCK_STATE_HOME"))
    parser.add_argument("--state-id", default=os.environ.get("A_STOCK_STATE_ID"))
    parser.add_argument("--env-file", default=os.environ.get("A_STOCK_ENV_FILE"))
    parser.add_argument("--openclaw", default="openclaw")
    parser.add_argument(
        "--delivery-channel",
        default=os.environ.get("A_STOCK_DELIVERY_CHANNEL", DEFAULT_DELIVERY_CHANNEL),
    )
    parser.add_argument("--delivery-to", default=os.environ.get("A_STOCK_DELIVERY_TO"))
    parser.add_argument(
        "--delivery-account",
        default=os.environ.get("A_STOCK_DELIVERY_ACCOUNT", DEFAULT_DELIVERY_ACCOUNT),
    )
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
    python = args.python or default_python(args.repo_dir)
    with open(args.manifest, encoding="utf-8") as handle:
        manifest = json.load(handle)
    installed = load_installed_openclaw_jobs(args.openclaw)
    validate_installed_job_uniqueness(installed)
    installed_jobs = installed if args.reconcile else None
    commands = build_openclaw_commands(
        manifest,
        repo_dir=os.path.abspath(args.repo_dir),
        python=os.path.abspath(python),
        grace_seconds=args.grace_seconds,
        state_home=args.state_home,
        state_id=args.state_id,
        env_file=args.env_file,
        installed_jobs=installed_jobs,
        openclaw=args.openclaw,
        delivery_channel=args.delivery_channel,
        delivery_to=args.delivery_to,
        delivery_account=args.delivery_account,
    )
    if args.apply:
        apply_openclaw_commands(commands)
    else:
        for command in commands:
            print(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
