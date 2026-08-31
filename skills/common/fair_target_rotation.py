"""Deterministic, persistent target rotation for bounded cron scans.

Priority targets are always selected.  Remaining capacity walks a stable stock
code ordering using a code anchor rather than a numeric offset, so additions
and removals do not reset the scan to the head of the list.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
from datetime import datetime
from typing import Any, Iterable, Mapping

from state_store import atomic_write_json, read_json


CURSOR_VERSION = 1


def _is_priority(target: Mapping[str, Any]) -> bool:
    if target.get("source") == "portfolio" or target.get("high_priority") is True:
        return True
    try:
        return float(target.get("priority") or 0) >= 90
    except (TypeError, ValueError):
        return False


def _normalized_targets(targets: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    for raw in targets:
        code = str(raw.get("code") or "").strip()
        if not code:
            continue
        target = dict(raw)
        target["code"] = code
        current = by_code.get(code)
        if current is None or (_is_priority(target) and not _is_priority(current)):
            by_code[code] = target
    return list(by_code.values())


def _read_cursor(path: str, job_id: str) -> tuple[str | None, str]:
    existed = os.path.exists(path)
    state = read_json(path, None)
    if not existed and state is None:
        return None, "new"
    if not isinstance(state, Mapping):
        return None, "invalid_reset"
    if state.get("version") != CURSOR_VERSION or state.get("job_id") != job_id:
        return None, "invalid_reset"
    next_code = state.get("next_code")
    if next_code is not None and not isinstance(next_code, str):
        return None, "invalid_reset"
    return next_code or None, "loaded"


def plan_fair_rotation(
    targets: Iterable[Mapping[str, Any]],
    *,
    max_targets: int,
    job_id: str,
    cursor_path: str,
) -> dict[str, Any]:
    """Plan one bounded scan without mutating cursor state."""
    normalized = _normalized_targets(targets)
    priority = sorted((row for row in normalized if _is_priority(row)), key=lambda row: row["code"])
    rotating = sorted((row for row in normalized if not _is_priority(row)), key=lambda row: row["code"])
    cursor_before, cursor_state = _read_cursor(cursor_path, job_id)

    capacity = max(0, int(max_targets) - len(priority))
    take = min(capacity, len(rotating))
    codes = [row["code"] for row in rotating]
    start = bisect.bisect_left(codes, cursor_before) if cursor_before else 0
    if start >= len(rotating):
        start = 0
    chosen = [rotating[(start + offset) % len(rotating)] for offset in range(take)] if rotating else []
    cursor_after = (
        rotating[(start + take) % len(rotating)]["code"]
        if rotating and take
        else cursor_before
    )
    selected = priority + chosen
    population_hash = hashlib.sha256(
        json.dumps(codes, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "job_id": job_id,
        "cursor_path": cursor_path,
        "targets": selected,
        "targets_total": len(normalized),
        "targets_scanned": len(selected),
        "targets_deferred": max(0, len(normalized) - len(selected)),
        "priority_scanned": len(priority),
        "cursor_before": cursor_before,
        "cursor_after": cursor_after,
        "cursor_state": cursor_state,
        "rotating_population_size": len(rotating),
        "rotating_population_hash": population_hash,
    }


def persist_rotation_cursor(plan: Mapping[str, Any]) -> None:
    """Commit the next anchor after the caller finishes its scan."""
    atomic_write_json(str(plan["cursor_path"]), {
        "version": CURSOR_VERSION,
        "job_id": plan["job_id"],
        "next_code": plan.get("cursor_after"),
        "rotating_population_size": plan.get("rotating_population_size", 0),
        "rotating_population_hash": plan.get("rotating_population_hash"),
        "updated_at": datetime.now().isoformat(),
    })


def rotation_metrics(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: plan.get(key)
        for key in (
            "targets_total",
            "targets_scanned",
            "targets_deferred",
            "priority_scanned",
            "cursor_before",
            "cursor_after",
            "cursor_state",
        )
    }
