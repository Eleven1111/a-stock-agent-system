"""Shared catalyst-event cache for four-dimension scoring.

Scheduled monitors can write already-fetched news and announcement events here.
The four-dimension scorer then consumes the cache so catalyst analysis is not
limited to a single live SerpAPI lookup.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Iterable, Mapping

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from paths import cache_dir  # noqa: E402
from state_store import mutate_json, read_json  # noqa: E402

DEFAULT_MAX_AGE_HOURS = 48


def context_file() -> str:
    return os.path.join(cache_dir("stock-triage"), "catalyst_context.json")


def normalize_code(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith(("sh", "sz", "bj")):
        raw = raw[2:]
    digits = "".join(ch for ch in raw if ch.isdigit())
    code = digits[-6:].zfill(6) if digits else ""
    return code if code.strip("0") else ""


def _event_key(event: Mapping[str, Any]) -> str:
    return str(event.get("link") or event.get("title") or event)


def _trim_events(events: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return events[: max(0, limit)]


def update_catalyst_context(
    events: Iterable[Mapping[str, Any]],
    *,
    generated_at: datetime | None = None,
    max_events_per_code: int = 50,
) -> dict[str, Any]:
    now = (generated_at or datetime.now()).isoformat(timespec="seconds")
    incoming: dict[str, list[dict[str, Any]]] = {}
    for raw in events:
        code = normalize_code(raw.get("stock_code") or raw.get("code"))
        if not code:
            continue
        item = dict(raw)
        item["stock_code"] = code
        item.setdefault("cached_at", now)
        incoming.setdefault(code, []).append(item)
    if not incoming:
        current = read_json(context_file(), {})
        return current if isinstance(current, dict) else {}

    def _mut(value: Any) -> dict[str, Any]:
        data = value if isinstance(value, dict) else {}
        stocks = data.get("stocks") if isinstance(data.get("stocks"), dict) else {}
        for code, items in incoming.items():
            existing = [
                item for item in stocks.get(code, [])
                if isinstance(item, dict)
            ]
            merged: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in [*items, *existing]:
                key = _event_key(item)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(dict(item))
            stocks[code] = _trim_events(merged, max_events_per_code)
        data.update({
            "schema": "catalyst_context_v1",
            "generated_at": now,
            "stocks": stocks,
        })
        return data

    return mutate_json(context_file(), _mut, {})


def read_catalyst_events(
    code: str,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    record = read_json(context_file(), None)
    if not isinstance(record, dict) or not record.get("generated_at"):
        return []
    try:
        fallback_at = datetime.fromisoformat(str(record["generated_at"]))
    except (TypeError, ValueError):
        fallback_at = None
    ref = now or datetime.now()
    stocks = record.get("stocks") if isinstance(record.get("stocks"), dict) else {}
    events = stocks.get(normalize_code(code), [])
    fresh: list[dict[str, Any]] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        event_at = fallback_at
        if item.get("cached_at"):
            try:
                event_at = datetime.fromisoformat(str(item["cached_at"]))
            except (TypeError, ValueError):
                event_at = fallback_at
        if event_at is None:
            continue
        if (ref - event_at).total_seconds() <= max_age_hours * 3600:
            fresh.append(dict(item))
    return fresh
