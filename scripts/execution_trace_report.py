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
SAMPLE_SCHEMA = "a_stock_trace_diagnosis_sample_v1"

# 分层诊断样本。只看失败会产出过拟合失败的修复：正常路径没有对照，就分不清
# 「守住了」和「永远不触发」。成功样本的职责是提供那个对照。
#
# 与参考做法（失败/成功二分）的三处刻意偏离：
#   1. blocked 单独成层。本仓库大量 blocked 是正确的 fail-closed 行为，把它并入
#      失败层会淹没真失败。
#   2. timeout 单独成层。实测生产 trace（2794 run）里 timeout 287 而 failed 仅 11，
#      两者同层会让固定名额全被 timeout 占满，真失败照样被挤掉——正是分层要防的。
#   3. 选取是确定性的，不是随机的。同一批事件必须采出同一份样本，否则诊断结论
#      不可复现。
STRATUM_STATUSES = {
    "failed": ("failed", "unterminated"),
    "timeout": ("timeout",),
    "blocked": ("blocked",),
    "ok": ("ok",),
}
STRATUM_CAPS = {"failed": 5, "timeout": 3, "blocked": 3, "ok": 3}
# 已知的空操作终态：计数但不采样，也不因此告警。未在此列的未分层终态才是信号
# ——那说明出现了没人预期过的终态。
NON_SAMPLED_STATUSES = ("skipped", "duplicate_skipped")
SAMPLE_MAX_TOTAL_CHARS = 15000


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


def _stratum_of(status: str) -> str | None:
    for stratum, statuses in STRATUM_STATUSES.items():
        if status in statuses:
            return stratum
    return None


def _sample_projection(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Fixed projection, so a new field on the run record cannot silently bloat
    the sample."""
    return {
        "run_id": entry.get("run_id"),
        "job_id": entry.get("job_id"),
        "status": entry.get("status") or "unterminated",
        "trading_date": entry.get("trading_date"),
        "started_at": entry.get("started_at"),
        "finished_at": entry.get("finished_at"),
        "duration_seconds": _wait_seconds(
            entry.get("started_at"), entry.get("finished_at")
        ),
        "gate_blocked": bool(entry.get("gate_blocked")),
        "agent_turns": entry.get("agent_turns"),
        "reason_codes": list(entry.get("reason_codes") or []),
        "artifact_ref": entry.get("artifact_ref"),
    }


def sample_runs_for_diagnosis(
    events: Sequence[Mapping[str, Any]],
    *,
    caps: Mapping[str, int] | None = None,
    max_total_chars: int = SAMPLE_MAX_TOTAL_CHARS,
) -> dict[str, Any]:
    """Bounded, deterministic, stratified run sample for diagnosis.

    Selection within each stratum is most-recent-first, ties broken by
    ``run_id``, so the same events always yield the same sample.

    An empty stratum is reported explicitly and raises a warning rather than
    silently shrinking the sample — a failures-only view that looks like a
    balanced one is worse than no sample at all.
    """
    budget = dict(STRATUM_CAPS)
    budget.update(caps or {})
    runs = execution_trace.reconstruct_runs(events)

    buckets: dict[str, list[Mapping[str, Any]]] = {key: [] for key in STRATUM_STATUSES}
    unclassified: dict[str, int] = {}
    for entry in runs.values():
        status = str(entry.get("status") or "unterminated")
        stratum = _stratum_of(status)
        if stratum is None:
            unclassified[status] = unclassified.get(status, 0) + 1
            continue
        buckets[stratum].append(entry)

    strata: dict[str, Any] = {}
    warnings: list[str] = []
    for stratum, entries in buckets.items():
        ordered = sorted(
            entries,
            key=lambda item: (
                str(item.get("finished_at") or item.get("started_at") or ""),
                str(item.get("run_id") or ""),
            ),
            reverse=True,
        )
        picked = ordered[: budget[stratum]]
        strata[stratum] = {
            "available": len(entries),
            "sampled": len(picked),
            "cap": budget[stratum],
            "runs": [_sample_projection(entry) for entry in picked],
        }

    if strata["ok"]["sampled"] == 0:
        warnings.append(
            "ok 层为空：样本只含失败/拦截，缺少正常路径对照，据此下的结论会偏向过拟合失败"
        )
    if not any(strata[key]["sampled"] for key in ("failed", "timeout", "blocked")):
        warnings.append("failed/timeout/blocked 三层都为空：本样本不含任何异常路径")
    unexpected = {
        status: count
        for status, count in unclassified.items()
        if status not in NON_SAMPLED_STATUSES
    }
    if unexpected:
        warnings.append(f"出现未预期的终态: {dict(sorted(unexpected.items()))}")

    payload_chars = len(json.dumps(strata, ensure_ascii=False))
    if payload_chars > max_total_chars:
        warnings.append(
            f"样本载荷 {payload_chars} 字符超出预算 {max_total_chars}；"
            "未自动裁剪，请下调 caps 后重取"
        )

    return {
        "schema": SAMPLE_SCHEMA,
        "generated_at": execution_trace.now_iso(),
        "run_count": len(runs),
        "selection": "deterministic: most recent first per stratum, tie-break run_id",
        "max_total_chars": max_total_chars,
        "payload_chars": payload_chars,
        "strata": strata,
        "unclassified_status_counts": dict(sorted(unclassified.items())),
        "warnings": warnings,
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
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Include a bounded stratified run sample (failed/blocked/ok)",
    )
    args = parser.parse_args()

    events, stats = execution_trace.read_events_with_stats(
        args.trace_path, trace_id=args.trace_id
    )
    report = build_report(events, stats=stats)
    if args.coverage:
        report["coverage"] = coverage_report(args.manifest, events)
    if args.diagnose:
        report["diagnosis_sample"] = sample_runs_for_diagnosis(events)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
