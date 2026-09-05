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


def _installed_by_name(
    installed_jobs: Sequence[Mapping[str, Any]] | None,
) -> dict[str, list[str]]:
    installed_by_name: dict[str, list[str]] = {}
    for installed in installed_jobs or ():
        name = str(installed.get("name") or "")
        installed_id = str(installed.get("id") or installed.get("jobId") or "")
        if name and installed_id:
            installed_by_name.setdefault(name, []).append(installed_id)
    return installed_by_name


def desired_job_spec(
    job_id: str,
    job: Mapping[str, Any],
    *,
    jobs: Mapping[str, Mapping[str, Any]],
    repo_dir: str,
    python: str,
    grace_seconds: int,
    command_env: Sequence[str],
    delivery_channel: str,
    delivery_to: str | None,
    delivery_account: str,
) -> dict[str, Any]:
    """The parameters this repository wants installed for ``job_id``.

    Single source of truth for both the emitted CLI command and the drift
    comparison, so an audit can never disagree with the generator about what
    "correct" means.
    """

    return {
        "logical_id": job_id,
        "name": f"{MANAGED_JOB_PREFIX}{job_id}",
        "schedule": str(job["schedule"]),
        "timezone": str(job.get("timezone") or "Asia/Shanghai"),
        "session": "isolated",
        "command_argv": [
            python, os.path.join(repo_dir, "scripts", "run_agent_dag.py"), job_id,
            "--runtime", "openclaw", "--emit-target",
        ],
        "command_cwd": repo_dir,
        "timeout_seconds": dependency_timeout_budget(
            jobs, job_id, grace_seconds=grace_seconds
        ),
        "output_max_bytes": max(4096, int(job.get("max_output_chars") or 2000) * 4),
        "command_env": list(command_env),
        "delivery_args": _delivery_args(
            job, channel=delivery_channel, target=delivery_to, account=delivery_account,
        ),
        "enabled": bool(job.get("enabled", True)),
    }


def command_from_spec(
    spec: Mapping[str, Any], *, openclaw: str, installed_id: str | None
) -> str:
    action = (
        ["edit", installed_id, "--name", spec["name"], "--cron", spec["schedule"]]
        if installed_id
        else ["create", spec["schedule"], "--name", spec["name"]]
    )
    parts = [openclaw, "cron", *action]
    parts.extend([
        "--tz", spec["timezone"],
        "--session", spec["session"],
        "--command-argv", json.dumps(spec["command_argv"], ensure_ascii=False),
        "--command-cwd", spec["command_cwd"],
        "--timeout-seconds", str(spec["timeout_seconds"]),
        "--output-max-bytes", str(spec["output_max_bytes"]),
        "--exact",
    ])
    for value in spec["command_env"]:
        parts.extend(["--command-env", value])
    parts.extend(spec["delivery_args"])
    return " ".join(shlex.quote(value) for value in parts)


def _job_command(
    job_id: str,
    job: Mapping[str, Any],
    *,
    jobs: Mapping[str, Mapping[str, Any]],
    installed_by_name: Mapping[str, Sequence[str]],
    repo_dir: str,
    python: str,
    openclaw: str,
    grace_seconds: int,
    command_env: Sequence[str],
    delivery_channel: str,
    delivery_to: str | None,
    delivery_account: str,
) -> str:
    spec = desired_job_spec(
        job_id, job, jobs=jobs, repo_dir=repo_dir, python=python,
        grace_seconds=grace_seconds, command_env=command_env,
        delivery_channel=delivery_channel, delivery_to=delivery_to,
        delivery_account=delivery_account,
    )
    matches = list(installed_by_name.get(spec["name"], ()))
    if len(matches) > 1:
        raise ValueError(
            f"duplicate installed OpenClaw jobs named {spec['name']}: {', '.join(matches)}"
        )
    return command_from_spec(
        spec, openclaw=openclaw, installed_id=matches[0] if matches else None
    )


def _command_env(
    state_home: str | None, state_id: str | None, env_file: str | None
) -> list[str]:
    """Environment forwarded into the command job.

    Secrets and recipient identities are loaded by the child runner from
    A_STOCK_ENV_FILE.  Never serialize their values into cron commands, dry-run
    output, process arguments, or the OpenClaw job store.
    """

    resolved = {
        "A_STOCK_STATE_HOME": state_home or os.environ.get("A_STOCK_STATE_HOME"),
        "A_STOCK_STATE_ID": state_id or os.environ.get("A_STOCK_STATE_ID"),
        "A_STOCK_ENV_FILE": env_file or os.environ.get("A_STOCK_ENV_FILE"),
        "A_STOCK_BACKUP_HOME": os.environ.get("A_STOCK_BACKUP_HOME", ""),
    }
    return [f"{key}={value}" for key, value in resolved.items() if value]


def _delivery_defaults(
    channel: str | None, target: str | None, account: str | None
) -> tuple[str, str | None, str]:
    return (
        channel or os.environ.get("A_STOCK_DELIVERY_CHANNEL") or DEFAULT_DELIVERY_CHANNEL,
        target or os.environ.get("A_STOCK_DELIVERY_TO"),
        account or os.environ.get("A_STOCK_DELIVERY_ACCOUNT") or DEFAULT_DELIVERY_ACCOUNT,
    )


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
    delivery_channel, delivery_to, delivery_account = _delivery_defaults(
        delivery_channel, delivery_to, delivery_account
    )
    jobs = {
        str(job["id"]): job
        for job in manifest.get("jobs", [])
        if isinstance(job, dict) and job.get("id")
    }
    installed = _installed_by_name(installed_jobs)
    command_env = _command_env(state_home, state_id, env_file)
    commands = []
    for job_id, job in jobs.items():
        if not job.get("enabled", True):
            continue
        # Secrets and recipient identities are loaded by the child runner from
        # A_STOCK_ENV_FILE. Never serialize their values into cron commands,
        # dry-run output, process arguments, or the OpenClaw job store.
        commands.append(_job_command(
            job_id, job, jobs=jobs, installed_by_name=installed,
            repo_dir=repo_dir, python=python, openclaw=openclaw,
            grace_seconds=grace_seconds, command_env=command_env,
            delivery_channel=delivery_channel, delivery_to=delivery_to,
            delivery_account=delivery_account,
        ))
    return commands


INSTALLED_FIELD_ALIASES: Mapping[str, tuple[str, ...]] = {
    "schedule": ("cron", "schedule", "expression", "cronExpression"),
    "timezone": ("tz", "timezone", "timeZone"),
    "session": ("session", "sessionMode"),
    "command_argv": ("commandArgv", "command_argv"),
    "command_cwd": ("commandCwd", "command_cwd"),
    "timeout_seconds": ("timeoutSeconds", "timeout_seconds"),
    "output_max_bytes": ("outputMaxBytes", "output_max_bytes"),
    "command_env": ("commandEnv", "command_env"),
}

DRIFT_FIELDS = tuple(INSTALLED_FIELD_ALIASES)


def _installed_value(installed: Mapping[str, Any], field: str) -> tuple[Any, bool]:
    """Look up ``field`` across the aliases an installed record may use.

    Returns ``(value, found)``.  A field the installed record simply does not
    report is *unknown*, never *drift*: the audit must not claim a difference it
    could not observe.
    """

    command = installed.get("command")
    for alias in INSTALLED_FIELD_ALIASES[field]:
        if alias in installed:
            return installed[alias], True
        if isinstance(command, Mapping):
            short = alias.removeprefix("command").removeprefix("command_")
            for candidate in (alias, short, short[:1].lower() + short[1:]):
                if candidate and candidate in command:
                    return command[candidate], True
    return None, False


def compare_installed_job(
    spec: Mapping[str, Any], installed: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """Field-by-field comparison of a desired spec against an installed job."""

    comparison: dict[str, dict[str, Any]] = {}
    for field in DRIFT_FIELDS:
        value, found = _installed_value(installed, field)
        desired = spec[field]
        if not found:
            state = "unknown"
        elif field in {"command_argv", "command_env"}:
            state = "match" if list(value or []) == list(desired) else "drift"
        else:
            state = "match" if str(value) == str(desired) else "drift"
        comparison[field] = {"desired": desired, "installed": value, "state": state}
    return comparison


def build_reconcile_plan(
    manifest: Mapping[str, Any],
    *,
    installed_jobs: Sequence[Mapping[str, Any]],
    repo_dir: str,
    python: str,
    grace_seconds: int = 60,
    state_home: str | None = None,
    state_id: str | None = None,
    env_file: str | None = None,
    openclaw: str = "openclaw",
    delivery_channel: str | None = None,
    delivery_to: str | None = None,
    delivery_account: str | None = None,
    disable_command_template: str | None = None,
) -> dict[str, Any]:
    """Differentiated create/update/disable/unchanged/conflict plan.

    Only jobs carrying this repository's managed prefix are ever acted on, and
    installed managed jobs whose logical ID has left the manifest entirely are
    reported as orphans rather than removed: ownership of a name is not the same
    as authorisation to delete it.
    """

    jobs = {
        str(job["id"]): job
        for job in manifest.get("jobs", [])
        if isinstance(job, dict) and job.get("id")
    }
    command_env = _command_env(state_home, state_id, env_file)
    channel, target, account = _delivery_defaults(
        delivery_channel, delivery_to, delivery_account
    )
    installed_by_name = _installed_records_by_name(installed_jobs)
    actions: list[dict[str, Any]] = []
    for job_id, job in jobs.items():
        actions.append(_planned_job(
            job_id, job, jobs=jobs,
            matches=installed_by_name.get(f"{MANAGED_JOB_PREFIX}{job_id}", []),
            repo_dir=repo_dir, python=python, grace_seconds=grace_seconds,
            command_env=command_env, channel=channel, target=target, account=account,
            openclaw=openclaw, disable_command_template=disable_command_template,
        ))
    orphans = sorted(
        name.removeprefix(MANAGED_JOB_PREFIX)
        for name in installed_by_name
        if name.removeprefix(MANAGED_JOB_PREFIX) not in jobs
    )
    actions.sort(key=lambda item: item["logical_id"])
    counts: dict[str, int] = {}
    for item in actions:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    return {
        "schema": "openclaw_reconcile_plan_v1",
        "managed_prefix": MANAGED_JOB_PREFIX,
        "actions": actions,
        "orphaned_managed_jobs": orphans,
        "summary": dict(sorted(counts.items())),
        "applicable": all(
            item["action"] != "conflict" and (
                item["action"] in {"unchanged", "skipped"} or item["command"]
            )
            for item in actions
        ),
    }


def _planned_job(
    job_id: str,
    job: Mapping[str, Any],
    *,
    jobs: Mapping[str, Mapping[str, Any]],
    matches: Sequence[Mapping[str, Any]],
    repo_dir: str,
    python: str,
    grace_seconds: int,
    command_env: Sequence[str],
    channel: str,
    target: str | None,
    account: str,
    openclaw: str,
    disable_command_template: str | None,
) -> dict[str, Any]:
    """One plan row, degrading to ``blocked`` instead of aborting the whole plan.

    A read-only plan has to be producible on a machine that has no delivery
    target: refusing to describe the other 48 jobs because 16 of them deliver to
    origin makes the diagnostic unusable exactly where it is needed.  The job is
    reported as blocked and ``applicable`` stays false, so nothing can be applied
    from an incomplete plan.
    """

    spec: dict[str, Any] | None = None
    if job.get("enabled", True):
        try:
            spec = desired_job_spec(
                job_id, job, jobs=jobs, repo_dir=repo_dir, python=python,
                grace_seconds=grace_seconds, command_env=command_env,
                delivery_channel=channel, delivery_to=target, delivery_account=account,
            )
        except ValueError as exc:
            return {
                "logical_id": job_id, "action": "blocked", "command": None,
                "reason": _plan_block_reason(exc),
                "installed_ids": [
                    str(item.get("id") or item.get("jobId") or item.get("job_id") or "")
                    for item in matches
                ],
            }
    return _plan_action(
        job_id, job, spec, matches,
        openclaw=openclaw, disable_command_template=disable_command_template,
    )


def _plan_block_reason(exc: ValueError) -> str:
    """Stable reason code for a job the plan could not describe.

    The exception text can carry the operator's configuration wording; only the
    classified code goes into the plan, which is a file people share.
    """

    message = str(exc)
    if "origin delivery target is required" in message:
        return "delivery_target_missing"
    if "unknown deliver policy" in message:
        return "deliver_policy_unknown"
    return "job_spec_invalid"


def _plan_action(
    job_id: str,
    job: Mapping[str, Any],
    spec: Mapping[str, Any] | None,
    matches: Sequence[Mapping[str, Any]],
    *,
    openclaw: str,
    disable_command_template: str | None,
) -> dict[str, Any]:
    base = {"logical_id": job_id, "installed_ids": [
        str(item.get("id") or item.get("jobId") or item.get("job_id") or "")
        for item in matches
    ]}
    if len(matches) > 1:
        return {
            **base, "action": "conflict", "command": None,
            "reason": "duplicate_installed_name",
        }
    installed = matches[0] if matches else None
    installed_id = base["installed_ids"][0] if matches else None
    if spec is None:
        if installed is None:
            return {**base, "action": "skipped", "command": None, "reason": "manifest_disabled"}
        # A job switched off in the manifest but still installed keeps firing on
        # the host until something disables it there.
        return {
            **base, "action": "disable",
            "command": (
                disable_command_template.format(openclaw=openclaw, job_id=installed_id)
                if disable_command_template else None
            ),
            "command_status": (
                "resolved" if disable_command_template else "unverified_cli_verb"
            ),
            "reason": "manifest_disabled_but_installed",
        }
    if installed is None:
        return {
            **base, "action": "create", "reason": "missing_from_openclaw",
            "command": command_from_spec(spec, openclaw=openclaw, installed_id=None),
        }
    comparison = compare_installed_job(spec, installed)
    drifted = sorted(key for key, value in comparison.items() if value["state"] == "drift")
    unknown = sorted(key for key, value in comparison.items() if value["state"] == "unknown")
    return {
        **base,
        "action": "update" if drifted else "unchanged",
        "reason": "parameter_drift" if drifted else "in_sync",
        "drifted_fields": drifted,
        "unverifiable_fields": unknown,
        "comparison": comparison,
        "command": (
            command_from_spec(spec, openclaw=openclaw, installed_id=installed_id)
            if drifted else None
        ),
    }


def _installed_records_by_name(
    installed_jobs: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for installed in installed_jobs:
        name = str(installed.get("name") or "")
        if name.startswith(MANAGED_JOB_PREFIX):
            grouped.setdefault(name, []).append(installed)
    return grouped


def _print_reconcile_plan(
    args: argparse.Namespace,
    manifest: Mapping[str, Any],
    installed: Sequence[Mapping[str, Any]],
    python: str,
) -> int:
    plan = build_reconcile_plan(
        manifest,
        installed_jobs=installed,
        repo_dir=os.path.abspath(args.repo_dir),
        python=os.path.abspath(python),
        grace_seconds=args.grace_seconds,
        state_home=args.state_home,
        state_id=args.state_id,
        env_file=args.env_file,
        openclaw=args.openclaw,
        delivery_channel=args.delivery_channel,
        delivery_to=args.delivery_to,
        delivery_account=args.delivery_account,
        disable_command_template=args.disable_command_template,
    )
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0 if plan["applicable"] else 1


def _build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Print a create/update/disable/unchanged/conflict plan as JSON instead of commands",
    )
    parser.add_argument(
        "--disable-command-template",
        default=os.environ.get("A_STOCK_OPENCLAW_DISABLE_TEMPLATE"),
        help=(
            "Command used to stop an installed job the manifest has disabled, e.g. "
            "'{openclaw} cron disable {job_id}'. Read the verb your installed version "
            "supports from 'openclaw cron --help'; without it the plan reports the "
            "disable action but leaves the command unresolved."
        ),
    )
    return parser


def main() -> int:
    parser = _build_parser()
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
    if args.plan:
        return _print_reconcile_plan(args, manifest, installed, python)
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
