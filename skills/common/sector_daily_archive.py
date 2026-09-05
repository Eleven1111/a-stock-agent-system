#!/usr/bin/env python3
"""Per-day, content-addressed archive for the four sector research artifacts.

The daily job writes only ``*_latest.json``, so two sessions later the D-day
cross-section is gone.  It is not recoverable from the cron artifact either:
``sector-crowding-daily`` caps its output at ``max_output_chars: 1500`` while a
full cross-section is dozens of sectors wide.  There is no other copy, so this
is the first one rather than a duplicate of something already retrievable.

Recomputing a past day writes a *new version* beside the old one.  Nothing is
overwritten, because "we recomputed it and the file changed" and "the history is
frozen" cannot both be true.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from paths import data_file
from research_artifact import json_sha256
from state_store import atomic_write_json, file_lock

SCHEMA = "sector_daily_archive_v1"
INDEX_NAME = "index.jsonl"
ARCHIVE_DIR = ("stock-triage", "sector_daily_archive")

ARTIFACT_NAMES = (
    "sector_crowding",
    "sector_price_factors",
    "sector_fake_breakout",
    "sector_rotation_pools",
)


def archive_root() -> Path:
    return Path(data_file(*ARCHIVE_DIR))


def _index_path() -> Path:
    return archive_root() / INDEX_NAME


def content_path(trading_date: str, artifact_name: str, digest: str) -> Path:
    return archive_root() / trading_date / f"{artifact_name}.{digest[:12]}.json"


def archive_day(
    trading_date: str,
    artifacts: Mapping[str, Mapping[str, Any]],
    *,
    inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Store one day's artifacts and append an index row naming their versions.

    Returns the index row.  Re-running an unchanged day is a no-op: the content
    address is identical, so nothing is written twice and no new version appears.
    """

    unknown = sorted(set(artifacts) - set(ARTIFACT_NAMES))
    if unknown:
        raise ValueError(f"unknown_sector_artifact:{','.join(unknown)}")
    versions: dict[str, str] = {}
    written: list[str] = []
    for name in ARTIFACT_NAMES:
        payload = artifacts.get(name)
        if payload is None:
            continue
        digest = json_sha256(dict(payload))
        versions[name] = digest
        path = content_path(trading_date, name, digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(str(path), dict(payload))
            written.append(str(path))
    row = {
        "schema": SCHEMA,
        "trading_date": trading_date,
        "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "versions": dict(sorted(versions.items())),
        "inputs": dict(inputs or {}),
        "paths": {
            name: str(content_path(trading_date, name, digest))
            for name, digest in sorted(versions.items())
        },
        "newly_written": written,
    }
    row["row_sha256"] = json_sha256(
        {key: value for key, value in row.items() if key not in {"archived_at", "row_sha256", "newly_written"}}
    )
    _append_index(row)
    return row


def _append_index(row: Mapping[str, Any]) -> None:
    path = _index_path()
    os.makedirs(path.parent, exist_ok=True)
    with file_lock(str(path)):
        existing = [
            item for item in read_index()
            if item.get("row_sha256") == row.get("row_sha256")
        ]
        if existing:
            return
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")


def read_index() -> list[dict[str, Any]]:
    path = _index_path()
    if not path.is_file():
        return []
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def versions_for(trading_date: str) -> list[dict[str, Any]]:
    """Every archived version of a day, oldest first.  More than one means it
    was recomputed, and both remain readable."""

    return [row for row in read_index() if row.get("trading_date") == trading_date]


def restore_day(trading_date: str, *, row_sha256: str | None = None) -> dict[str, Any]:
    """Reload a day's four artifacts, defaulting to its most recent version."""

    rows = versions_for(trading_date)
    if not rows:
        return {"status": "unavailable", "reason": "no_archived_version", "trading_date": trading_date}
    row = rows[-1]
    if row_sha256 is not None:
        matched = [item for item in rows if item.get("row_sha256") == row_sha256]
        if not matched:
            return {"status": "unavailable", "reason": "version_not_archived", "trading_date": trading_date}
        row = matched[0]
    artifacts: dict[str, Any] = {}
    missing: list[str] = []
    for name, path in (row.get("paths") or {}).items():
        candidate = Path(str(path))
        if not candidate.is_file():
            missing.append(name)
            continue
        with open(candidate, encoding="utf-8") as handle:
            artifacts[name] = json.load(handle)
    return {
        "status": "ok" if not missing else "partial",
        "trading_date": trading_date,
        "row_sha256": row.get("row_sha256"),
        "version_count": len(rows),
        "inputs": row.get("inputs") or {},
        "artifacts": artifacts,
        "missing": sorted(missing),
    }


__all__ = [
    "ARTIFACT_NAMES", "SCHEMA", "archive_day", "archive_root", "content_path",
    "read_index", "restore_day", "versions_for",
]
