"""Intraday sector-strength time series persistence.

``intraday-alert`` already recomputes every tracked sector's strength every 15
minutes, but the result used to be written to the daily alert-dedup cache with a
plain overwrite -- each tick discarded the previous frame, so no intraday
trajectory ever survived. This module turns that single frame into a persisted,
per-trading-day series so downstream stages can ask time-series questions
("did morning strength hold into the afternoon?", "are new members still
joining?") instead of re-scoring a single instant.

Pure functions plus bounded state writes only: callers supply an already
computed sector-state mapping, nothing here fetches quotes or reaches the
network.

Three fail-closed properties are the point of this module, not decoration:

1. **"the job never ran" must stay distinguishable from "the job ran but had no
   usable observation."** ``slots`` records every slot the collector actually
   executed; ``degraded_slots`` records the subset that executed without usable
   members. A slot missing from ``slots`` is an ops failure; a slot present in
   both is a data failure. Silently skipping the write on a degraded tick would
   make those two indistinguishable (the issue #112/#113 lesson).
2. **Gaps are never interpolated.** The slope helper delegates to
   ``market_temperature.three_day_slope``, which returns ``None`` below three
   observations rather than passing zero off as "flat".
3. **An empty series never yields a confident-looking number.**
   ``derive_persistence`` returns ``insufficient_slots`` instead of a ratio
   computed over an empty set.
"""

from __future__ import annotations

import glob
import os
from datetime import datetime
from typing import Any, Mapping

from market_temperature import three_day_slope
from paths import skill_data_dir
from state_store import mutate_json, read_json


SCHEMA = "sector_intraday_series_v1"
SLOT_MINUTES = 15
DEFAULT_KEEP_DAYS = 20
_SERIES_SUBDIR = "sector_series"

# Fields copied from a detect_sector_acceleration frame into the series. Kept
# explicit so an upstream field addition cannot silently bloat every slot.
_SLOT_FIELDS = (
    "average_pct",
    "positive_ratio",
    "member_count",
    "alerted",
    "participation_scope",
)


def series_dir() -> str:
    return os.path.join(skill_data_dir("stock-triage"), _SERIES_SUBDIR)


def series_file(asof: str) -> str:
    return os.path.join(series_dir(), f"{asof}.json")


def slot_of(now: datetime) -> str:
    """Floor an execution timestamp into its 15-minute bucket.

    cron jitter and retries land a few seconds either side of the nominal slot;
    bucketing makes a re-run overwrite its own slot instead of appending a
    near-duplicate frame.
    """
    return f"{now.hour:02d}:{(now.minute // SLOT_MINUTES) * SLOT_MINUTES:02d}"


def load_day(asof: str) -> dict[str, Any]:
    value = read_json(series_file(asof), {})
    return value if isinstance(value, dict) else {}


def _slot_entry(slot: str, state: Mapping[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"t": slot}
    for key in _SLOT_FIELDS:
        if state.get(key) is not None:
            entry[key] = state[key]
    return entry


def record_slot(
    asof: str,
    slot: str,
    sector_state: Mapping[str, Mapping[str, Any]] | None,
    *,
    degraded_reason: str | None = None,
    keep_days: int = DEFAULT_KEEP_DAYS,
) -> dict[str, Any]:
    """Upsert one 15-minute frame into the day's series.

    Idempotent per slot: re-running the same bucket replaces that slot
    everywhere rather than appending. The replacement purges the slot from
    *every* sector first, so a re-run that observes fewer sectors cannot leave a
    stale entry behind from the earlier run -- the latest run for a slot is
    authoritative.

    ``degraded_reason`` records a tick that executed without usable members. The
    slot still enters ``slots`` (it did run); it additionally enters
    ``degraded_slots`` so consumers can tell the two failure modes apart.
    """
    state = dict(sector_state or {})
    first_slot_holder: dict[str, bool] = {}

    def _mut(current: Any) -> dict[str, Any]:
        data = dict(current) if isinstance(current, Mapping) else {}
        data["schema"] = SCHEMA
        data["asof"] = asof

        slots = [item for item in (data.get("slots") or []) if isinstance(item, str)]
        first_slot_holder["first"] = not slots
        data["slots"] = sorted({*slots, slot})

        degraded = [
            dict(item)
            for item in (data.get("degraded_slots") or [])
            if isinstance(item, Mapping) and str(item.get("t") or "") != slot
        ]
        if degraded_reason:
            degraded.append({"t": slot, "reason": str(degraded_reason)})
        data["degraded_slots"] = sorted(degraded, key=lambda item: str(item.get("t") or ""))

        sectors: dict[str, list[dict[str, Any]]] = {}
        for name, entries in (data.get("sectors") or {}).items():
            kept = [
                dict(entry)
                for entry in (entries or [])
                if isinstance(entry, Mapping) and str(entry.get("t") or "") != slot
            ]
            if kept:
                sectors[str(name)] = kept
        for name, frame in state.items():
            if not isinstance(frame, Mapping):
                continue
            series = sectors.setdefault(str(name), [])
            series.append(_slot_entry(slot, frame))
            series.sort(key=lambda entry: str(entry.get("t") or ""))
        data["sectors"] = sectors
        return data

    updated = mutate_json(series_file(asof), _mut, default={})
    if first_slot_holder.get("first"):
        prune_old_days(keep_days=keep_days)
    return updated


def derive_persistence(day: Mapping[str, Any] | None, sector: str) -> dict[str, Any]:
    """Bounded trajectory summary for one sector within the current day.

    ``average_pct_slope`` stays ``None`` below three observed slots -- a two
    point "trend" is not evidence, and reporting 0.0 there would read as
    "flat" rather than "unknown".
    """
    data = dict(day or {})
    slots = [item for item in (data.get("slots") or []) if isinstance(item, str)]
    entries = [
        entry
        for entry in ((data.get("sectors") or {}).get(sector) or [])
        if isinstance(entry, Mapping)
    ]
    if not slots or not entries:
        return {
            "status": "insufficient_slots",
            "observed_slot_count": len(entries),
            "recorded_slot_count": len(slots),
            "observed_slot_ratio": None,
            "average_pct_slope": None,
            "member_delta": None,
        }
    averages = [entry.get("average_pct") for entry in entries]
    members = [entry.get("member_count") for entry in entries if entry.get("member_count") is not None]
    return {
        "status": "ok",
        "observed_slot_count": len(entries),
        "recorded_slot_count": len(slots),
        "observed_slot_ratio": round(len(entries) / len(slots), 4),
        "average_pct_slope": three_day_slope(averages),
        "member_delta": (members[-1] - members[0]) if len(members) >= 2 else None,
    }


def summarize_day(day: Mapping[str, Any] | None, *, limit: int = 5) -> dict[str, Any]:
    """Artifact-safe counters only.

    ``intraday-alert`` caps stdout at 2500 chars, so the series body must never
    reach the artifact; callers get counts plus at most ``limit`` sector names.
    """
    data = dict(day or {})
    sectors = data.get("sectors") or {}
    return {
        "schema": SCHEMA,
        "recorded_slot_count": len([s for s in (data.get("slots") or []) if isinstance(s, str)]),
        "degraded_slot_count": len(data.get("degraded_slots") or []),
        "tracked_sector_count": len(sectors),
        "tracked_sectors": sorted(sectors)[:limit],
    }


def prune_old_days(*, keep_days: int = DEFAULT_KEEP_DAYS) -> int:
    """Drop all but the newest ``keep_days`` daily files; returns the count removed."""
    if keep_days <= 0:
        return 0
    paths = sorted(glob.glob(os.path.join(series_dir(), "*.json")))
    removed = 0
    for path in paths[:-keep_days] if len(paths) > keep_days else []:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            continue
    return removed
