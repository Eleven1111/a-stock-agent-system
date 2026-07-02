#!/usr/bin/env python3
"""Read-only audit for Hermes/OpenClaw dual-runtime duplicate execution risk.

Run this on every machine that might be executing cron jobs (Hermes host,
OpenClaw host, or both) and compare the JSON output. It never mutates state:
no leases are claimed, no jobs are run, no files are written.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)
sys.path.insert(0, ROOT)

from paths import hermes_home  # noqa: E402
from runtime_context import ledger_path  # noqa: E402
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
    """Flag (job_id, trading_date, batch_id) groups completed by >1 runtime
    within window_seconds of each other — evidence of the same node actually
    executing twice instead of the lease deduplicating it."""
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
        timestamps = sorted(
            _parse_ts(m.get("finished_at") or m.get("started_at")) for m in members
        )
        timestamps = [t for t in timestamps if t is not None]
        if len(timestamps) < 2:
            continue
        spread = (max(timestamps) - min(timestamps)).total_seconds()
        if spread <= window_seconds:
            findings.append({
                "job_id": job_id,
                "trading_date": trading_date,
                "batch_id": batch_id,
                "runtimes": sorted(runtimes),
                "run_count": len(members),
                "spread_seconds": spread,
            })
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


def active_leases(state_root: str) -> list[dict[str, Any]]:
    """List leases currently held (i.e. a job actively mid-run right now,
    or one whose lease was never released due to a crash)."""
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
                holder = read_json(holder_path, {})
                age_seconds = None
                try:
                    age_seconds = round(
                        datetime.now().timestamp() - os.stat(lease_dir).st_mtime, 1
                    )
                except OSError:
                    pass
                held.append({
                    "job_id": job_id.removesuffix(".lease"),
                    "trading_date": trading_date,
                    "batch_id": batch_id,
                    "runtime": holder.get("runtime"),
                    "acquired_at": holder.get("acquired_at"),
                    "age_seconds": age_seconds,
                })
    return held


def state_identity_summary(state_root: str) -> dict[str, Any]:
    identity = read_json(os.path.join(state_root, "state_identity.json"), {})
    return {
        "state_root": state_root,
        "state_id": identity.get("state_id"),
        "created_at": identity.get("created_at"),
        "initial_root": identity.get("initial_root"),
        "matches_current_root": identity.get("initial_root") == state_root
        if identity.get("initial_root")
        else None,
    }


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
    installed_ids = {str(job.get("id") or job.get("job_id") or "") for job in installed}
    manifest_ids = {
        str(job.get("id"))
        for job in manifest.get("jobs", [])
        if job.get("enabled", True) and job.get("deliver") != "silent"
    }
    return {
        "status": "ok",
        "installed_count": len(installed_ids),
        "manifest_enabled_count": len(manifest_ids),
        "missing_from_openclaw": sorted(manifest_ids - installed_ids),
        "orphaned_in_openclaw": sorted(installed_ids - manifest_ids),
    }


def build_report(
    manifest: dict[str, Any],
    runs: list[dict[str, Any]],
    *,
    state_root: str,
    window_seconds: int = 300,
    check_openclaw: bool = True,
) -> dict[str, Any]:
    duplicates = detect_concurrent_duplicate_runs(runs, window_seconds=window_seconds)
    leases = active_leases(state_root)
    report = {
        "schema": "a_stock_dual_runtime_audit_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state_identity": state_identity_summary(state_root),
        "runtime_distribution": runtime_distribution(runs),
        "sample_run_count": len(runs),
        "concurrent_duplicate_runs": duplicates,
        "active_leases": leases,
        "openclaw_registration": (
            openclaw_registration_check(manifest) if check_openclaw else {"status": "skipped"}
        ),
    }
    report["clean"] = not duplicates and not leases
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
    runs = read_json(runs_path, [])
    if not isinstance(runs, list):
        runs = []

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
