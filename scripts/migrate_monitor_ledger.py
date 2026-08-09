#!/usr/bin/env python3
"""
Migrate monitor.* lifecycle events out of the signal ledger.

The signal ledger (``signal_ledger.jsonl``) is the canonical decision ledger.
Historically monitor.activated/deactivated churn was appended there too,
dominating the file (~97% of events) and driving O(N)-per-write degradation.
This tool moves every ``event_type`` starting with ``monitor.`` into the new
``monitor_ledger.jsonl`` and atomically rewrites the signal ledger without them.

Critical trap — backup mirror:
    signal_ledger keeps a mirror under ``backup_home()`` and restores the whole
    ledger from it when the main file is missing. If the mirror still carries
    monitor events, a future restore would re-inject them. This tool therefore
    rewrites the mirror too (atomic + snapshot).

Safety:
    - --dry-run (default) never mutates anything; --apply performs the rewrite.
    - The whole operation runs under file_lock on the signal ledger.
    - A ``*.pre-migration-<timestamp>`` snapshot is copied before any rewrite.
    - Any failure leaves the original files untouched (fail-closed): each
      rewrite writes to a tmp file first and only os.replace()s on success.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import monitor_ledger  # noqa: E402
import signal_ledger  # noqa: E402
from state_store import file_lock  # noqa: E402


def _is_monitor_event(event: Any) -> bool:
    return (
        isinstance(event, dict)
        and str(event.get("event_type") or "").startswith("monitor.")
    )


def _partition(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    monitor_events = [event for event in events if _is_monitor_event(event)]
    kept = [event for event in events if not _is_monitor_event(event)]
    return monitor_events, kept


def _snapshot(path: str, timestamp: str) -> str | None:
    if not os.path.exists(path):
        return None
    snapshot = f"{path}.pre-migration-{timestamp}"
    shutil.copy2(path, snapshot)
    return snapshot


def _atomic_rewrite(path: str, events: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.migrate.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, default=str))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def migrate(
    *,
    apply: bool,
    signal_ledger_file: str | None = None,
    monitor_ledger_file: str | None = None,
) -> dict[str, Any]:
    ledger_path = signal_ledger_file or signal_ledger.LEDGER_FILE
    monitor_path = monitor_ledger_file or monitor_ledger.LEDGER_FILE
    mirror_path = signal_ledger._ledger_backup_path(ledger_path)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")

    result: dict[str, Any] = {
        "apply": apply,
        "signal_ledger": ledger_path,
        "monitor_ledger": monitor_path,
        "mirror": mirror_path,
        "migrated_events": 0,
        "kept_events": 0,
        "mirror_migrated_events": 0,
        "mirror_kept_events": 0,
        "snapshots": [],
    }

    with file_lock(ledger_path):
        main_events = signal_ledger._read_events_unlocked(ledger_path)
        main_monitor, main_kept = _partition(main_events)
        result["migrated_events"] = len(main_monitor)
        result["kept_events"] = len(main_kept)

        mirror_monitor: list[dict[str, Any]] = []
        mirror_kept: list[dict[str, Any]] = []
        if mirror_path and os.path.exists(mirror_path):
            mirror_events = signal_ledger._read_events_unlocked(mirror_path)
            mirror_monitor, mirror_kept = _partition(mirror_events)
            result["mirror_migrated_events"] = len(mirror_monitor)
            result["mirror_kept_events"] = len(mirror_kept)
            result["mirror_processed"] = True
        else:
            result["mirror_processed"] = False

        if not apply:
            result["dry_run"] = True
            return result

        if not main_monitor and not mirror_monitor:
            result["dry_run"] = False
            result["noop"] = True
            return result

        snapshots: list[str] = []
        main_snapshot = _snapshot(ledger_path, timestamp)
        if main_snapshot:
            snapshots.append(main_snapshot)
        mirror_snapshot = None
        if mirror_path and mirror_monitor:
            mirror_snapshot = _snapshot(mirror_path, timestamp)
            if mirror_snapshot:
                snapshots.append(mirror_snapshot)
        result["snapshots"] = snapshots

        # Migrate events out first (append-only, recoverable via snapshot on
        # later failure); then rewrite the sources. De-duplicate main vs mirror
        # by event_id so a mirrored copy of a main event is not migrated twice.
        migrated_out = list(main_monitor)
        seen_ids = {event.get("event_id") for event in main_monitor}
        for event in mirror_monitor:
            event_id = event.get("event_id")
            if event_id is None or event_id not in seen_ids:
                migrated_out.append(event)
                seen_ids.add(event_id)
        if migrated_out:
            monitor_ledger.append_events(migrated_out, ledger_file=monitor_path)

        _atomic_rewrite(ledger_path, main_kept)
        if mirror_path and mirror_monitor:
            _atomic_rewrite(mirror_path, mirror_kept)

        result["dry_run"] = False
        result["noop"] = False
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        help="report what would move without touching files (default)",
    )
    group.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        help="perform the migration",
    )
    parser.set_defaults(apply=False)
    parser.add_argument("--signal-ledger", default=None)
    parser.add_argument("--monitor-ledger", default=None)
    args = parser.parse_args()

    result = migrate(
        apply=args.apply,
        signal_ledger_file=args.signal_ledger,
        monitor_ledger_file=args.monitor_ledger,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
