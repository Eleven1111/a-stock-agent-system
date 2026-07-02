"""Adaptive polling backoff for high-frequency, mostly-idle cron jobs.

Lets a job's effective cadence follow observed signal rate instead of pure
wall-clock time: a job that has gone quiet for a while gets skipped on some
ticks, while any tick that finds a real signal immediately resets it back
to full frequency (the has_signal escape hatch).

This module only decides and records; it never touches the job's own
business logic or output.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paths import hermes_home
from state_store import mutate_json

SCHEMA = "adaptive_schedule_v1"

# (min_miss_streak, run_every_n_ticks), ascending; last matching threshold wins.
BACKOFF_STEPS: tuple[tuple[int, int], ...] = (
    (0, 1),
    (3, 2),
    (9, 4),
    (21, 8),
)


def _default_state() -> dict[str, Any]:
    return {"schema": SCHEMA, "jobs": {}}


def _default_entry() -> dict[str, Any]:
    return {"miss_streak": 0, "ticks_since_run": 0}


def state_path() -> Path:
    return Path(hermes_home()) / "runtime" / "adaptive_schedule.json"


def _interval_for_streak(streak: int) -> int:
    interval = 1
    for threshold, step in BACKOFF_STEPS:
        if streak >= threshold:
            interval = step
    return interval


def should_run(
    job_id: str,
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Advance job_id's tick counter and decide whether this tick is due.

    Safe to call unconditionally, including when the caller will run the
    job regardless (shadow mode) -- it only records what the policy would
    have decided.
    """
    current = now or datetime.now(timezone.utc)
    target = Path(path) if path else state_path()

    def _mutate(state: Any) -> dict[str, Any]:
        if not isinstance(state, dict) or state.get("schema") != SCHEMA:
            state = _default_state()
        jobs = state.setdefault("jobs", {})
        entry = dict(_default_entry(), **(jobs.get(job_id) or {}))
        entry["ticks_since_run"] = int(entry.get("ticks_since_run") or 0) + 1
        jobs[job_id] = entry
        return state

    state = mutate_json(str(target), _mutate, _default_state())
    entry = state["jobs"][job_id]
    streak = int(entry.get("miss_streak") or 0)
    interval = _interval_for_streak(streak)
    ticks_since_run = int(entry.get("ticks_since_run") or 0)
    due = ticks_since_run >= interval
    return {
        "job_id": job_id,
        "miss_streak": streak,
        "interval": interval,
        "ticks_since_run": ticks_since_run,
        "run": due,
        "would_skip": not due,
        "evaluated_at": current.isoformat(timespec="seconds"),
    }


def record_outcome(
    job_id: str,
    *,
    ran: bool,
    has_signal: bool | None,
    path: str | Path | None = None,
) -> None:
    """Update streak/tick bookkeeping after a tick resolves.

    ran=False means the job was actually skipped (enforce mode): no new
    observation happened, so the miss streak is left untouched -- only an
    actual run can extend or reset it.
    """
    if not ran or has_signal is None:
        return
    target = Path(path) if path else state_path()

    def _mutate(state: Any) -> dict[str, Any]:
        if not isinstance(state, dict) or state.get("schema") != SCHEMA:
            state = _default_state()
        jobs = state.setdefault("jobs", {})
        entry = dict(_default_entry(), **(jobs.get(job_id) or {}))
        entry["ticks_since_run"] = 0
        entry["miss_streak"] = 0 if has_signal else int(entry.get("miss_streak") or 0) + 1
        jobs[job_id] = entry
        return state

    mutate_json(str(target), _mutate, _default_state())
