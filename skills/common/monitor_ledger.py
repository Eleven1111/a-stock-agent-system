#!/usr/bin/env python3
"""
Append-only ledger for monitor lifecycle churn.

Monitor activation/deactivation events are high-frequency state transitions
(1000-1500/day) that must NOT live in ``signal_ledger.jsonl`` — the canonical
decision ledger. Keeping them separate avoids the O(N)-per-write dedup and
backup-mirror sync that the signal ledger performs, and keeps the audit surface
of the signal ledger readable.

This module is deliberately minimal:
- pure append, no full-file dedup scan (each event is naturally unique via
  its timestamp);
- no backup mirror (monitor state is rebuildable from ``monitor_registry.json``
  and is not CRITICAL data).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from paths import data_file
from state_store import file_lock


LEDGER_FILE = data_file("stock-triage", "monitor_ledger.jsonl")
SCHEMA = "monitor_ledger_event_v1"
COMPATIBLE_SCHEMAS = {SCHEMA}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_event(
    event_type: str,
    links: Mapping[str, Any],
    payload: Optional[Mapping[str, Any]],
    occurred_at: Optional[str],
) -> dict[str, Any]:
    normalized_type = str(event_type).strip()
    if not normalized_type:
        raise ValueError("monitor ledger event requires event_type")
    return {
        "schema": SCHEMA,
        "event_type": normalized_type,
        "occurred_at": occurred_at or _now(),
        "links": dict(links or {}),
        "payload": dict(payload or {}),
    }


def append_event(
    event_type: str,
    links: Mapping[str, Any],
    payload: Optional[Mapping[str, Any]] = None,
    *,
    occurred_at: Optional[str] = None,
    ledger_file: Optional[str] = None,
) -> dict[str, Any]:
    """Append one monitor event; pure append, no full-file dedup scan."""
    path = ledger_file or LEDGER_FILE
    event = _normalize_event(event_type, links, payload, occurred_at)
    with file_lock(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    return event


def append_events(
    events: Iterable[Mapping[str, Any]],
    ledger_file: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Append a batch of monitor events in one lock; pure append."""
    path = ledger_file or LEDGER_FILE
    normalized = [
        _normalize_event(
            event["event_type"],
            event.get("links") or {},
            event.get("payload") or {},
            event.get("occurred_at"),
        )
        for event in events
    ]
    if not normalized:
        return []
    with file_lock(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            for event in normalized:
                handle.write(json.dumps(event, ensure_ascii=False, default=str))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    return normalized


def _read_events_unlocked(ledger_file: str) -> list[dict[str, Any]]:
    if not os.path.exists(ledger_file):
        return []
    events = []
    with open(ledger_file, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("schema") in COMPATIBLE_SCHEMAS:
                events.append(value)
    return events


def read_events(ledger_file: Optional[str] = None) -> list[dict[str, Any]]:
    """Read and parse monitor events, tolerating and skipping corrupt lines."""
    path = ledger_file or LEDGER_FILE
    with file_lock(path):
        return _read_events_unlocked(path)
