#!/usr/bin/env python3
"""Report observed P95/P99 and theoretical OpenClaw command-cron budgets."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from typing import Any, Iterable

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)
sys.path.insert(0, ROOT)

from runtime_context import ledger_path  # noqa: E402
from scripts.generate_openclaw_cron import dependency_timeout_budget  # noqa: E402
from state_store import read_json  # noqa: E402


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def build_budget_report(
    manifest: dict[str, Any],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    jobs = {str(job["id"]): job for job in manifest.get("jobs", [])}
    durations: dict[str, list[float]] = defaultdict(list)
    for run in runs:
        if run.get("status") != "ok":
            continue
        duration = run.get("duration_seconds")
        if isinstance(duration, (int, float)) and duration >= 0:
            durations[str(run.get("job_id"))].append(float(duration))
    rows = {}
    for job_id, job in jobs.items():
        if not job.get("enabled", True):
            continue
        samples = durations.get(job_id, [])
        rows[job_id] = {
            "samples": len(samples),
            "observed_p95_seconds": _percentile(samples, 0.95),
            "observed_p99_seconds": _percentile(samples, 0.99),
            "job_timeout_seconds": int(
                (job.get("run") or {}).get("timeout_seconds") or 120
            ),
            "dependency_worst_case_seconds": dependency_timeout_budget(
                jobs,
                job_id,
            ),
        }
    return {
        "schema": "a_stock_cron_budget_report_v1",
        "status": "ok",
        "jobs": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="cron/hermes-cron-manifest.json")
    parser.add_argument("--runs", default=ledger_path())
    args = parser.parse_args()
    with open(args.manifest, encoding="utf-8") as handle:
        manifest = json.load(handle)
    runs = read_json(args.runs, [])
    if not isinstance(runs, list):
        runs = []
    print(json.dumps(build_budget_report(manifest, runs), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
