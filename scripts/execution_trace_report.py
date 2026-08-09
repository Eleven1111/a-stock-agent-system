#!/usr/bin/env python3
"""Report on the shadow execution trace.

The trace is written in shadow mode: it observes runs and never gates them.
This report is how that shadow data becomes a decision — it answers whether the
trace is complete enough to rely on, not whether the business results were good.

Shadow gate signals produced here:

- ``trace_gaps``: a run with no start, no terminal, or more than one terminal.
- ``delivery``: attempts versus provider acceptances. Acceptance is *not* a user
  receipt and is never reported as one.
- ``duration_p95``: run-time distribution, to check the trace has not slowed the
  scheduler beyond the agreed margin.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Any, Mapping, Sequence

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import execution_trace  # noqa: E402

REPORT_SCHEMA = "a_stock_execution_trace_report_v1"


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 3)


def _wait_seconds(start: Any, end: Any) -> float | None:
    try:
        first = datetime.fromisoformat(str(start))
        second = datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return None
    return round((second - first).total_seconds(), 3)


def _aggregate_runs(
    runs: Mapping[str, Mapping[str, Any]],
    dispatch_claims: Mapping[str, Any],
) -> dict[str, Any]:
    """Fold per-run entries into counters, latencies and blocked reasons."""
    status_counts: dict[str, int] = {}
    durations: list[float] = []
    dependency_waits: list[float] = []
    blocked_reasons: dict[str, int] = {}
    per_job: dict[str, dict[str, int]] = {}

    for entry in runs.values():
        status = str(entry.get("status") or "unterminated")
        status_counts[status] = status_counts.get(status, 0) + 1
        job_id = str(entry.get("job_id") or "unknown")
        job_bucket = per_job.setdefault(job_id, {})
        job_bucket[status] = job_bucket.get(status, 0) + 1
        elapsed = _wait_seconds(entry.get("started_at"), entry.get("finished_at"))
        if elapsed is not None and elapsed >= 0:
            durations.append(elapsed)
        claimed_at = dispatch_claims.get(str(entry.get("trace_id")))
        waited = _wait_seconds(claimed_at, entry.get("started_at"))
        if waited is not None and waited >= 0:
            dependency_waits.append(waited)
        if entry.get("gate_blocked") or status.startswith("blocked"):
            for code in entry.get("reason_codes") or ["unspecified"]:
                blocked_reasons[code] = blocked_reasons.get(code, 0) + 1

    return {
        "status_counts": status_counts,
        "durations": durations,
        "dependency_waits": dependency_waits,
        "blocked_reasons": blocked_reasons,
        "per_job": per_job,
    }


def build_report(
    events: Sequence[Mapping[str, Any]],
    *,
    stats: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    runs = execution_trace.reconstruct_runs(events)
    gaps = execution_trace.find_gaps(events)
    dispatch_claims = {
        str(event.get("trace_id")): event.get("occurred_at")
        for event in events
        if event.get("event_type") == "dispatch.claimed"
    }
    folded = _aggregate_runs(runs, dispatch_claims)
    status_counts = folded["status_counts"]
    durations = folded["durations"]
    dependency_waits = folded["dependency_waits"]
    blocked_reasons = folded["blocked_reasons"]
    per_job = folded["per_job"]

    terminated = sum(
        count for status, count in status_counts.items() if status != "unterminated"
    )
    attempted = sum(entry["delivery_attempts"] for entry in runs.values())
    accepted = sum(entry["delivery_accepted"] for entry in runs.values())
    failed = sum(entry["delivery_failed"] for entry in runs.values())

    return {
        "schema": REPORT_SCHEMA,
        "generated_at": execution_trace.now_iso(),
        "event_count": len(events),
        "read_stats": dict(stats or {}),
        "run_count": len(runs),
        "completion_rate": round(terminated / len(runs), 4) if runs else None,
        "status_counts": dict(sorted(status_counts.items())),
        "per_job_status": {job: dict(sorted(v.items())) for job, v in sorted(per_job.items())},
        "blocked_reason_codes": dict(
            sorted(blocked_reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "duration_seconds": {
            "count": len(durations),
            "p50": _percentile(durations, 0.5),
            "p95": _percentile(durations, 0.95),
            "max": round(max(durations), 3) if durations else None,
        },
        "dispatch_to_start_seconds": {
            "count": len(dependency_waits),
            "p50": _percentile(dependency_waits, 0.5),
            "p95": _percentile(dependency_waits, 0.95),
        },
        "delivery": {
            "attempted": attempted,
            "provider_accepted": accepted,
            "failed": failed,
            "provider_acceptance_rate": (
                round(accepted / attempted, 4) if attempted else None
            ),
            "receipt_known": False,
            "note": "provider acceptance is not a user receipt; no receipt source exists",
        },
        "trace_gaps": gaps,
        "shadow_gate": {
            "no_duplicate_terminal": not any(
                gap["gap"] == "duplicate_terminal" for gap in gaps
            ),
            "no_terminal_without_start": not any(
                gap["gap"] == "missing_start" for gap in gaps
            ),
            "no_fabricated_receipt": True,
        },
    }


def coverage_report(
    manifest_path: str,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Which enabled manifest jobs have ever produced a terminal trace event."""
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    enabled = [
        str(job.get("id"))
        for job in manifest.get("jobs", [])
        if job.get("enabled")
    ]
    seen = {
        str(event.get("job_id"))
        for event in events
        if event.get("event_type") in execution_trace.TERMINAL_EVENTS
    }
    missing = sorted(job_id for job_id in enabled if job_id not in seen)
    return {
        "enabled_jobs": len(enabled),
        "jobs_with_terminal_event": len(enabled) - len(missing),
        "missing_jobs": missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-path", default=None)
    parser.add_argument("--trace-id", default=None)
    parser.add_argument(
        "--manifest",
        default=os.path.join(ROOT, "cron", "hermes-cron-manifest.json"),
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Include per-manifest-job trace coverage",
    )
    args = parser.parse_args()

    events, stats = execution_trace.read_events_with_stats(
        args.trace_path, trace_id=args.trace_id
    )
    report = build_report(events, stats=stats)
    if args.coverage:
        report["coverage"] = coverage_report(args.manifest, events)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
