#!/usr/bin/env python3
"""Read-only audit for Hermes/OpenClaw dual-runtime duplicate execution risk.

Run this on every machine that might be executing cron jobs (Hermes host,
OpenClaw host, or both) and compare the JSON output. It never mutates state:
no leases are claimed, no jobs are run, no files are written -- lease holders are
read without the locking helper precisely because that helper would create a
lock file inside the directory it is about to measure.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from itertools import combinations
from typing import Any, Mapping

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
sys.path.insert(0, ROOT)

from paths import hermes_home  # noqa: E402
import run_lease  # noqa: E402
from runtime_context import ledger_path  # noqa: E402
from scripts.generate_openclaw_cron import MANAGED_JOB_PREFIX  # noqa: E402
from state_store import read_json  # noqa: E402


def runtime_distribution(runs: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for run in runs:
        counts[str(run.get("runtime") or "unknown")] += 1
    return dict(counts)


def detect_concurrent_duplicate_runs(
    runs: list[dict[str, Any]],
    *,
    window_seconds: int = 300,
) -> list[dict[str, Any]]:
    """Flag cross-runtime overlap for the same logical batch and job."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        if run.get("status") != "ok":
            continue
        key = (
            str(run.get("job_id") or ""),
            str(run.get("trading_date") or ""),
            str(run.get("batch_id") or ""),
        )
        groups[key].append(run)

    findings = []
    for (job_id, trading_date, batch_id), members in groups.items():
        runtimes = {str(m.get("runtime") or "unknown") for m in members}
        if len(runtimes) < 2:
            continue
        basis = None
        overlap_seconds = None
        for left, right in combinations(members, 2):
            if str(left.get("runtime") or "unknown") == str(
                right.get("runtime") or "unknown"
            ):
                continue
            left_interval = _run_interval(left)
            right_interval = _run_interval(right)
            if left_interval and right_interval:
                overlap = (
                    min(left_interval[1], right_interval[1])
                    - max(left_interval[0], right_interval[0])
                ).total_seconds()
                if overlap >= 0:
                    basis = "interval_overlap"
                    overlap_seconds = overlap
                    break
                continue
            left_point = _parse_ts(left.get("finished_at") or left.get("started_at"))
            right_point = _parse_ts(right.get("finished_at") or right.get("started_at"))
            if left_point and right_point:
                spread = abs((left_point - right_point).total_seconds())
                if spread <= window_seconds:
                    basis = "completion_window"
                    break
        if basis is None:
            continue
        timestamps = [
            timestamp
            for timestamp in (
                _parse_ts(member.get("finished_at") or member.get("started_at"))
                for member in members
            )
            if timestamp is not None
        ]
        finding = {
            "job_id": job_id,
            "trading_date": trading_date,
            "batch_id": batch_id,
            "runtimes": sorted(runtimes),
            "run_count": len(members),
            "spread_seconds": (
                (max(timestamps) - min(timestamps)).total_seconds()
                if len(timestamps) >= 2
                else None
            ),
            "detection_basis": basis,
        }
        if overlap_seconds is not None:
            finding["overlap_seconds"] = overlap_seconds
        findings.append(finding)
    return findings


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _run_interval(run: dict[str, Any]) -> tuple[datetime, datetime] | None:
    started = _parse_ts(run.get("started_at"))
    finished = _parse_ts(run.get("finished_at"))
    if started is None or finished is None or finished < started:
        return None
    return started, finished


def _read_holder_unlocked(holder_path: str) -> dict[str, Any]:
    """Read a lease holder without creating a lock file beside it."""
    try:
        with open(holder_path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _lease_age_seconds(lease_dir: str, holder: Mapping[str, Any]) -> float | None:
    """Age from the holder's own immutable timestamp, falling back to mtime.

    ``acquired_at`` is written once by ``run_lease.claim`` and never rewritten,
    so it survives anything that touches the directory afterwards.
    """
    acquired = _parse_ts(holder.get("acquired_at"))
    if acquired is not None:
        return round((datetime.now(timezone.utc) - acquired).total_seconds(), 1)
    try:
        return round(datetime.now().timestamp() - os.stat(lease_dir).st_mtime, 1)
    except OSError:
        return None


def active_leases(
    state_root: str,
    *,
    ttl_seconds: int = run_lease.DEFAULT_TTL_SECONDS,
    today: str | None = None,
) -> list[dict[str, Any]]:
    """List leases on disk, marking the ones that can no longer belong to a run.

    ``run_lease`` only reclaims a stale lease when a new claim contends for the
    *same* ``(job_id, trading_date, batch_id)`` path.  A lease left behind for a
    trading date that has passed is never contended again, so nothing in the
    system will ever clear it -- and an audit that counts it as held stays
    unclean forever, which blocks the very canary the runbook gates on it.

    A lease is only called orphaned when *both* signals agree: it is older than
    the module's own reclaim TTL, and its trading date is behind today.  A long
    job running for the current session exceeds the TTL too, and must not be
    written off as an orphan."""
    leases_root = os.path.join(state_root, "runtime", "leases")
    held = []
    if not os.path.isdir(leases_root):
        return held
    for trading_date in os.listdir(leases_root):
        date_dir = os.path.join(leases_root, trading_date)
        if not os.path.isdir(date_dir):
            continue
        for batch_id in os.listdir(date_dir):
            batch_dir = os.path.join(date_dir, batch_id)
            if not os.path.isdir(batch_dir):
                continue
            for job_id in os.listdir(batch_dir):
                lease_dir = os.path.join(batch_dir, job_id)
                holder_path = os.path.join(lease_dir, "holder.json")
                if not os.path.isfile(holder_path):
                    continue
                # Read without the lock helper on purpose. ``read_json`` opens
                # ``<path>.lock`` with O_CREAT, which lands *inside* the lease
                # directory and updates the very mtime measured on the next line
                # -- every lease then reports the same age, namely the time since
                # the previous audit run. It also made this script's "never
                # mutates state" promise untrue.
                holder = _read_holder_unlocked(holder_path)
                age_seconds = _lease_age_seconds(lease_dir, holder)
                reasons = []
                if age_seconds is not None and age_seconds > ttl_seconds:
                    reasons.append("older_than_reclaim_ttl")
                if trading_date < (today or date.today().isoformat()):
                    reasons.append("trading_date_in_the_past")
                held.append({
                    "job_id": job_id.removesuffix(".lease"),
                    "trading_date": trading_date,
                    "batch_id": batch_id,
                    "runtime": holder.get("runtime"),
                    "acquired_at": holder.get("acquired_at"),
                    "age_seconds": age_seconds,
                    "orphaned": len(reasons) == 2,
                    "orphan_reasons": reasons,
                })
    return held


def _active_leases_inventory(
    state_root: str, today: str | None = None
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    try:
        return active_leases(state_root, today=today), {"status": "ok"}
    except (OSError, TimeoutError):
        return [], {"status": "error", "reason": "lease inventory query failed"}


def state_identity_summary(state_root: str) -> dict[str, Any]:
    identity = read_json(os.path.join(state_root, "state_identity.json"), None)
    if not isinstance(identity, dict):
        return {
            "status": "error",
            "state_root": state_root,
            "reason": "state identity query failed",
        }
    summary = {
        "state_root": state_root,
        "state_id": identity.get("state_id"),
        "created_at": identity.get("created_at"),
        "initial_root": identity.get("initial_root"),
        "matches_current_root": identity.get("initial_root") == state_root
        if identity.get("initial_root")
        else None,
    }
    summary["status"] = (
        "ok"
        if summary["state_id"] and summary["matches_current_root"] is True
        else "error"
    )
    return summary


def openclaw_registration_check(manifest: dict[str, Any], openclaw: str = "openclaw") -> dict[str, Any]:
    binary = shutil.which(openclaw)
    if not binary:
        return {"status": "unavailable", "reason": "openclaw binary not found on this machine"}
    try:
        completed = subprocess.run(
            [openclaw, "cron", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "reason": str(exc)}
    if completed.returncode != 0:
        return {
            "status": "error",
            "reason": (completed.stderr or completed.stdout or "unknown error").strip()[:500],
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "reason": "openclaw cron list returned invalid JSON"}
    installed = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(installed, list):
        installed = []
    managed_names: dict[str, list[str]] = defaultdict(list)
    for job in installed:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or "")
        if not name.startswith(MANAGED_JOB_PREFIX):
            continue
        logical_id = name.removeprefix(MANAGED_JOB_PREFIX).strip()
        if logical_id:
            managed_names[logical_id].append(
                str(job.get("id") or job.get("jobId") or job.get("job_id") or "")
            )
    installed_ids = set(managed_names)
    # Match the generator exactly: it installs every enabled job and maps the
    # non-origin deliver policies (local/silent/feishu_direct) to --no-deliver.
    # Filtering "silent" out here would report a correctly installed job as an
    # orphan the moment one is enabled.
    manifest_ids = {
        str(job.get("id"))
        for job in manifest.get("jobs", [])
        if job.get("enabled", True)
    }
    manifest_disabled_ids = {
        str(job.get("id"))
        for job in manifest.get("jobs", [])
        if not job.get("enabled", True)
    }
    return {
        "status": "ok",
        "installed_count": len(installed_ids),
        "manifest_enabled_count": len(manifest_ids),
        "missing_from_openclaw": sorted(manifest_ids - installed_ids),
        "disabled_but_installed": sorted(installed_ids & manifest_disabled_ids),
        "orphaned_in_openclaw": sorted(
            installed_ids - manifest_ids - manifest_disabled_ids
        ),
        "duplicate_managed_names": sorted(
            logical_id for logical_id, ids in managed_names.items() if len(ids) > 1
        ),
    }


def build_report(
    manifest: dict[str, Any],
    runs: Any,
    *,
    state_root: str,
    window_seconds: int = 300,
    check_openclaw: bool = True,
    today: str | None = None,
) -> dict[str, Any]:
    runs_valid = isinstance(runs, list) and all(isinstance(run, dict) for run in runs)
    normalized_runs = runs if runs_valid else []
    duplicates = detect_concurrent_duplicate_runs(
        normalized_runs,
        window_seconds=window_seconds,
    )
    leases, lease_inventory = _active_leases_inventory(state_root, today)
    state_identity = state_identity_summary(state_root)
    registration = (
        openclaw_registration_check(manifest) if check_openclaw else {"status": "skipped"}
    )
    runtime_inventory = {
        "status": "ok" if runs_valid else "error",
        "reason": None if runs_valid else "run inventory is not a list of objects",
    }
    orphaned = [item for item in leases if item.get("orphaned")]
    genuinely_held = [item for item in leases if not item.get("orphaned")]
    report = {
        "schema": "a_stock_dual_runtime_audit_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state_identity": state_identity,
        "runtime_inventory": runtime_inventory,
        "lease_inventory": lease_inventory,
        "runtime_distribution": runtime_distribution(normalized_runs),
        "sample_run_count": len(normalized_runs),
        "concurrent_duplicate_runs": duplicates,
        "active_leases": genuinely_held,
        "orphaned_leases": orphaned,
        "orphaned_lease_note": (
            "run_lease only reclaims a stale lease when the same "
            "(job_id, trading_date, batch_id) is claimed again, which never "
            "happens once the trading date has passed; these need an explicit "
            "operator cleanup and do not indicate a running job"
        ),
        "openclaw_registration": registration,
    }
    registration_blocked = registration.get("status") not in {"ok", "skipped"} or any(
        registration.get(key)
        for key in (
            "missing_from_openclaw",
            "orphaned_in_openclaw",
            "disabled_but_installed",
            "duplicate_managed_names",
        )
    )
    query_blocked = any(
        component.get("status") != "ok"
        for component in (state_identity, runtime_inventory, lease_inventory)
    ) or registration_blocked
    # Orphans are reported, not counted as held: leaving them to block `clean`
    # forever would gate the canary on a condition nothing can ever satisfy.
    report["clean"] = not query_blocked and not duplicates and not genuinely_held
    report["status"] = (
        "blocked" if query_blocked
        else ("findings" if duplicates or genuinely_held or orphaned else "ok")
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=os.path.join(ROOT, "cron", "hermes-cron-manifest.json"))
    parser.add_argument("--runs", default=None, help="Override job_runs.json path")
    parser.add_argument("--window-seconds", type=int, default=300)
    parser.add_argument("--no-openclaw-check", action="store_true")
    args = parser.parse_args()

    with open(args.manifest, encoding="utf-8") as handle:
        manifest = json.load(handle)
    state_root = hermes_home()
    runs_path = args.runs or ledger_path()
    runs = read_json(runs_path, None)

    report = build_report(
        manifest,
        runs,
        state_root=state_root,
        window_seconds=args.window_seconds,
        check_openclaw=not args.no_openclaw_check,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
