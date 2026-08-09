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
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
sys.path.insert(0, ROOT)

from runtime_context import ledger_path  # noqa: E402
from scripts.generate_openclaw_cron import dependency_timeout_budget  # noqa: E402
from state_store import read_json  # noqa: E402
from paths import hermes_home  # noqa: E402


#: Headroom a manifest timeout should keep over the observed p95. Purely
#: multiplicative on purpose: ``dependency_timeout_budget`` adds a flat 60s
#: grace because it sums a whole chain, but per job that constant would flag
#: every short job unconditionally (production paper-trading-monitor runs a
#: 0.717s p95 under a 30s timeout and would "need" 62s).
MARGIN_FACTOR = 3.0

#: Below this many samples the empirical p95 is literally ``max(samples)``:
#: with k < 20 the estimator index collapses onto the last element, and the
#: chance that a k-sample maximum even reaches the true 95th percentile is
#: 1 - 0.95^k — 40% at k=10. Ten samples is also ~two trading weeks for a
#: daily job. Fewer than that yields an advisory, never a verdict.
MIN_MARGIN_SAMPLES = 10


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _margin(
    samples: int,
    p95: float | None,
    timeout: int,
) -> tuple[str, int, float | None]:
    """Classify one job's timeout headroom against its observed p95.

    Returns ``(status, recommended_timeout_seconds, margin_ratio)``. The
    recommendation is ``max(manifest_timeout, ceil(p95 * MARGIN_FACTOR))`` — it
    may raise a timeout and never lower one. That asymmetry is not politeness:
    the sample set only contains ``status == "ok"`` runs, and until PR #162 a
    timed-out run left no artifact at all, so history holds the fast successes
    and none of the slow deaths. Any p95 computed here is a floor.
    """
    if not p95 or samples <= 0:
        return "no_samples", timeout, None
    recommended = max(timeout, math.ceil(p95 * MARGIN_FACTOR))
    ratio = round(timeout / p95, 3)
    if samples < MIN_MARGIN_SAMPLES:
        return "insufficient_samples", recommended, ratio
    if recommended > timeout:
        return "thin_margin", recommended, ratio
    return "ok", recommended, ratio


def _margin_entry(job_id: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "level": row["margin_status"],
        "samples": row["samples"],
        "observed_p95_seconds": row["observed_p95_seconds"],
        "job_timeout_seconds": row["job_timeout_seconds"],
        "recommended_timeout_seconds": row["recommended_timeout_seconds"],
        "margin_ratio": row["margin_ratio"],
    }


def _margin_lists(
    rows: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split thin-margin jobs into confirmed warnings and sample-poor hints."""
    warnings: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []
    for job_id, row in rows.items():
        thin = row["recommended_timeout_seconds"] > row["job_timeout_seconds"]
        if row["margin_status"] == "thin_margin":
            warnings.append(_margin_entry(job_id, row))
        elif row["margin_status"] == "insufficient_samples" and thin:
            advisories.append(_margin_entry(job_id, row))
    warnings.sort(key=lambda item: (item["margin_ratio"], item["job_id"]))
    advisories.sort(key=lambda item: (item["margin_ratio"], item["job_id"]))
    return warnings, advisories


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
        p95 = _percentile(samples, 0.95)
        timeout = int((job.get("run") or {}).get("timeout_seconds") or 120)
        status, recommended, ratio = _margin(len(samples), p95, timeout)
        rows[job_id] = {
            "samples": len(samples),
            "observed_p95_seconds": p95,
            "observed_p99_seconds": _percentile(samples, 0.99),
            "job_timeout_seconds": timeout,
            "margin_ratio": ratio,
            "margin_status": status,
            "recommended_timeout_seconds": recommended,
            "dependency_worst_case_seconds": dependency_timeout_budget(
                jobs,
                job_id,
            ),
        }
    warnings, advisories = _margin_lists(rows)
    return {
        "schema": "a_stock_cron_budget_report_v1",
        "status": "ok",
        "margin_policy": {
            "margin_factor": MARGIN_FACTOR,
            "min_samples": MIN_MARGIN_SAMPLES,
            "adjustment": "raise_only",
            "sample_bias": "ok_runs_only",
        },
        "warnings": warnings,
        "advisories": advisories,
        "jobs": rows,
    }


def push_telemetry_path() -> str:
    return os.path.join(hermes_home(), "cron", "push_telemetry.jsonl")


def read_push_telemetry(path: str | None = None) -> list[dict[str, Any]]:
    telemetry_path = path or push_telemetry_path()
    if not os.path.exists(telemetry_path):
        return []
    records: list[dict[str, Any]] = []
    with open(telemetry_path, encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return 0


def build_push_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    daily_total: dict[str, int] = defaultdict(int)
    char_by_job: dict[str, int] = defaultdict(int)

    for raw in records:
        job_id = str(raw.get("job_id") or "").strip()
        trading_date = str(raw.get("trading_date") or "").strip()
        if not job_id or not trading_date:
            continue
        output_chars = _positive_int(raw.get("output_chars"))
        record = {
            "job_id": job_id,
            "trading_date": trading_date,
            "delivered": raw.get("delivered") is True,
            "output_chars": output_chars,
            "was_compressed": raw.get("was_compressed") is True,
            "silent_reason": str(raw.get("silent_reason") or "none"),
        }
        by_job[job_id].append(record)
        char_by_job[job_id] += output_chars
        daily_total[trading_date] += output_chars

    jobs: dict[str, dict[str, Any]] = {}
    for job_id in sorted(by_job):
        samples = by_job[job_id]
        days = {item["trading_date"] for item in samples}
        day_count = max(1, len(days))
        delivered = sum(1 for item in samples if item["delivered"])
        total_chars = sum(item["output_chars"] for item in samples)
        silent = sum(1 for item in samples if not item["delivered"])
        compressed = sum(1 for item in samples if item["was_compressed"])
        runs = len(samples)
        jobs[job_id] = {
            "sample_days": len(days),
            "runs": runs,
            "daily_avg_pushes": round(delivered / day_count, 3),
            "daily_avg_chars": round(total_chars / day_count, 3),
            "silent_rate": round(silent / runs, 3),
            "compression_rate": round(compressed / runs, 3),
        }

    top5 = [
        {"job_id": job_id, "output_chars": output_chars}
        for job_id, output_chars in sorted(
            char_by_job.items(),
            key=lambda item: (-item[1], item[0]),
        )[:5]
    ]
    return {
        "schema": "a_stock_push_telemetry_report_v1",
        "status": "ok",
        "jobs": jobs,
        "char_top5": top5,
        "daily_total_push_chars": {
            day: daily_total[day]
            for day in sorted(daily_total)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="cron/hermes-cron-manifest.json")
    parser.add_argument("--runs", default=ledger_path())
    parser.add_argument("--push-report", action="store_true")
    parser.add_argument("--push-telemetry", default=push_telemetry_path())
    parser.add_argument(
        "--fail-on-warn",
        action="store_true",
        help=(
            "exit 1 when a job's timeout is within "
            f"{MARGIN_FACTOR}x of its observed p95 (>= {MIN_MARGIN_SAMPLES} "
            "samples). Advisories never fail: too few samples is not a verdict."
        ),
    )
    args = parser.parse_args()
    if args.push_report:
        print(json.dumps(
            build_push_report(read_push_telemetry(args.push_telemetry)),
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    with open(args.manifest, encoding="utf-8") as handle:
        manifest = json.load(handle)
    runs = read_json(args.runs, [])
    if not isinstance(runs, list):
        runs = []
    report = build_budget_report(manifest, runs)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.fail_on_warn and report["warnings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
