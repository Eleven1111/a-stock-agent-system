#!/usr/bin/env python3
"""Retention holds for evidence a live experiment still needs.

Ordinary GC protection comes from ``_scan_recent_references``, which only reads
files modified inside ``reference_protection_days`` (30).  A pre-registered
experiment freezes its inputs on day zero and then does not touch that record
again, so after a month its precommit stops protecting anything -- while a
research cycle of 60 fitting plus 60 out-of-sample trading days runs for roughly
170 calendar days.  The evidence would be collected out from under a study that
is still running.

A hold is an explicit, append-only statement that some snapshot paths must
survive regardless of file age.  Releases are appended too: the ledger is a
history, not a mutable set, so "what was protected on day N and why" stays
answerable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from paths import hermes_home
from research_artifact import json_sha256
from state_store import file_lock

SCHEMA = "retention_hold_v1"
LEDGER_RELATIVE = ("research", "retention_holds.jsonl")

RECORD_HOLD = "hold"
RECORD_RELEASE = "release"


def ledger_path(state_home: str | Path | None = None) -> Path:
    home = Path(state_home or hermes_home()).expanduser()
    return home.joinpath(*LEDGER_RELATIVE)


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_ledger(state_home: str | Path | None = None) -> list[dict[str, Any]]:
    path = ledger_path(state_home)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("schema") == SCHEMA:
                rows.append(value)
    return rows


def _append(record: Mapping[str, Any], state_home: str | Path | None) -> dict[str, Any]:
    path = ledger_path(state_home)
    os.makedirs(path.parent, exist_ok=True)
    row = dict(record)
    row["record_sha256"] = json_sha256(
        {key: value for key, value in row.items() if key != "record_sha256"}
    )
    with file_lock(str(path)):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def place_hold(
    scope: str,
    references: Iterable[str | Path],
    *,
    reason: str,
    expires_at: str | None = None,
    state_home: str | Path | None = None,
) -> dict[str, Any]:
    """Declare that ``references`` must survive GC until the hold is released.

    ``expires_at`` is optional and, when absent, the hold lasts until an explicit
    release.  A study whose end date is unknown must not quietly expire on a
    guessed one.
    """

    paths = sorted({str(Path(item)) for item in references})
    if not scope or not reason or not paths:
        raise ValueError("retention_hold_incomplete")
    return _append(
        {
            "schema": SCHEMA,
            "record_type": RECORD_HOLD,
            "scope": scope,
            "reason": reason,
            "references": paths,
            "expires_at": expires_at,
            "placed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        state_home,
    )


def release_hold(
    scope: str, *, reason: str, state_home: str | Path | None = None
) -> dict[str, Any]:
    """Append a release; the original hold row stays readable."""

    if not scope or not reason:
        raise ValueError("retention_release_incomplete")
    return _append(
        {
            "schema": SCHEMA,
            "record_type": RECORD_RELEASE,
            "scope": scope,
            "reason": reason,
            "released_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        state_home,
    )


def active_holds(
    *, now: datetime | None = None, state_home: str | Path | None = None
) -> list[dict[str, Any]]:
    """Holds that are neither released nor past their own expiry."""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    latest: dict[str, dict[str, Any]] = {}
    for row in read_ledger(state_home):
        scope = str(row.get("scope") or "")
        if not scope:
            continue
        latest[scope] = row
    active = []
    for row in latest.values():
        if row.get("record_type") != RECORD_HOLD:
            continue
        expiry = _parse(row.get("expires_at"))
        if expiry is not None and expiry <= current:
            continue
        active.append(row)
    return sorted(active, key=lambda item: str(item.get("scope")))


def held_references(
    *, now: datetime | None = None, state_home: str | Path | None = None
) -> set[Path]:
    """Every path an active hold protects, resolved for comparison with GC."""

    held: set[Path] = set()
    for row in active_holds(now=now, state_home=state_home):
        for item in row.get("references") or []:
            held.add(Path(str(item)).expanduser().resolve(strict=False))
    return held


def describe(
    *, now: datetime | None = None, state_home: str | Path | None = None
) -> dict[str, Any]:
    """Report shape for the GC plan: what is held, by whom, and why."""

    holds = active_holds(now=now, state_home=state_home)
    return {
        "schema": "retention_hold_report_v1",
        "active_scopes": [str(row.get("scope")) for row in holds],
        "held_reference_count": sum(len(row.get("references") or []) for row in holds),
        "reasons": {str(row.get("scope")): str(row.get("reason")) for row in holds},
        "expiring": {
            str(row.get("scope")): row.get("expires_at")
            for row in holds
            if row.get("expires_at")
        },
    }


def scope_for_experiment(experiment_id: str, experiment_sha256: str) -> str:
    return f"experiment:{experiment_id}:{str(experiment_sha256)[:16]}"


def hold_experiment_evidence(
    experiment: Mapping[str, Any],
    references: Sequence[str | Path],
    *,
    state_home: str | Path | None = None,
) -> dict[str, Any]:
    """Convenience wrapper binding a hold to a frozen experiment identity."""

    return place_hold(
        scope_for_experiment(
            str(experiment["experiment_id"]), str(experiment["experiment_sha256"])
        ),
        references,
        reason="active_pre_registered_experiment",
        state_home=state_home,
    )


__all__ = [
    "RECORD_HOLD", "RECORD_RELEASE", "SCHEMA", "active_holds", "describe",
    "held_references", "hold_experiment_evidence", "ledger_path", "place_hold",
    "read_ledger", "release_hold", "scope_for_experiment",
]
