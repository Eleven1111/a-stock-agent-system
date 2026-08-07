#!/usr/bin/env python3
"""
Archive superseded monitor lifecycle events out of the canonical signal ledger.

Why (issue #167)
    ``signal_ledger.jsonl`` is append-only and 98.7% of it is monitor.* churn
    (12078 events / 14MB over 24 days, +500~800/day). Every process that touches
    the monitor registry replays the whole file once, so the cost grows with
    calendar time — the reason three successive timeout bumps all failed.

What is archived
    Only ``monitor.*`` events that are BOTH
      (a) older than the retention window, and
      (b) fully superseded — every field they carry is re-stated by a later
          retained event for the same monitor_id.
    Non-monitor events (recommendation / paper / signal — 157 of 12078) are
    never archived: they are rare, cheap, and the long-lived audit trail.

Why (b) makes this safe for the fail-closed registry check
    ``monitor_registry._registry_projection_matches_ledger`` folds the ledger
    with ``event_projection.fold_monitor_records`` (last-write-wins per key,
    merge-don't-replace) and requires the result to be a subset of the registry.
    Rule (b) guarantees the LAST event carrying any given key survives, so the
    folded projection is bit-identical before and after archiving — the check
    keeps exactly the same strength instead of merely "still passing on a
    smaller expectation". The script proves this per run rather than assuming
    it: ``_verify`` compares the two folds and aborts on any difference.

Safety
    - --dry-run is the default; --apply is explicit.
    - Everything is computed and verified in memory BEFORE any file is touched.
    - ``*.pre-archive-<timestamp>`` snapshots of the main ledger and its mirror
      are taken first; the rollback commands are printed in the report.
    - The backup mirror is rewritten in lockstep, otherwise a later
      ``_restore_ledger_unlocked`` would re-inject the archived events.
    - Post-write the files are re-read and re-verified; a failure restores the
      snapshots and reports ok=false.

Scheduling
    Deliberately NOT registered in cron/hermes-cron-manifest.json yet: this
    rewrites the canonical ledger and has never run against production. Run it
    supervised (scheduler stopped) first. Once that run is proven, register it
    as a weekly job — schedule "0 9 * * 6", trading_day_policy "calendar_day",
    run.argv ["python", "skills/common/signal_ledger_archive.py", "--apply"],
    timeout_tier "standard" (60s on 14MB, so 120s is ample) — and add the row to
    AUTOPILOT.md in the same change.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

import event_projection
import signal_ledger
from state_store import file_lock


SCHEMA = "signal_ledger_archive_v1"
# 保留窗口默认 7 天。依据（不是拍脑袋）：
#  1) 折叠等价性保证注册表校验强度与窗口无关，窗口只决定「原始事件在主账本里
#     还能被人肉回看多久」；
#  2) grep 全仓确认 monitor.* 事件除 monitor_registry / event_projection 外没有
#     任何消费者，其余脚本一律按 signal.* / paper.* / recommendation.* 过滤；
#  3) 与 monitor 生命周期相关的最长运维回看窗口是
#     performance_tracker.TERMINAL_UNRESOLVED_DAYS = 7（其余更短：
#     AGED_PENDING_DAYS=3、recommendation_audit max_age_days=4、
#     daban position_time_stop_trading_days=2）；
#  4) 原始 churn 并不会因此丢失：monitor_registry._record_monitor_event 把每条
#     monitor 事件同时写进 monitor_ledger.jsonl 兼容镜像，归档文件是第三份。
DEFAULT_RETENTION_DAYS = 7
ARCHIVE_DIR_NAME = "signal_ledger.archive"


def _is_monitor(event: Mapping[str, Any]) -> bool:
    return str(event.get("event_type") or "").startswith("monitor.")


def _occurred_day(event: Mapping[str, Any]) -> str:
    return str(event.get("occurred_at") or "")[:10]


def _archivable_ids(events: list[dict[str, Any]], cutoff: str) -> set[str]:
    """Superseded + out-of-window monitor events, walking newest to oldest.

    ``covered`` holds the keys already re-stated by a retained later event of the
    same monitor. An event whose keys are all covered contributes nothing to the
    fold, so dropping it cannot change the projection.
    """
    covered: dict[str, set[str]] = {}
    archivable: set[str] = set()
    for event in reversed(events):
        if not _is_monitor(event):
            continue
        monitor_id, entry = event_projection._monitor_entry(event)
        seen = covered.setdefault(monitor_id, set())
        if set(entry) <= seen and _occurred_day(event) < cutoff:
            archivable.add(str(event.get("event_id")))
        else:
            seen.update(entry)
    return archivable


def _partition(
    events: list[dict[str, Any]], archivable: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    retained = [e for e in events if str(e.get("event_id")) not in archivable]
    archived = [e for e in events if str(e.get("event_id")) in archivable]
    return retained, archived


def _fold_by_id(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        str(record.get("id")): record
        for record in event_projection.fold_monitor_records([], events)
    }


def _sequences(events: Iterable[Mapping[str, Any]]) -> list[int]:
    """Effective sequences, using the same fallback as signal_ledger.append_events.

    Legacy ``signal_ledger_event_v1`` rows carry no ``sequence`` (5319 of the
    12078 production events); append_events falls back to the 1-based line
    number for them. Archiving shifts those line numbers, so the check below has
    to reason in the same units — otherwise it would either report phantom
    duplicates or, worse, miss a real sequence reuse on a legacy-only ledger.
    """
    return [
        int(event.get("sequence") or index)
        for index, event in enumerate(events, start=1)
    ]


def _verify(
    original: list[dict[str, Any]],
    retained: list[dict[str, Any]],
    archived: list[dict[str, Any]],
    cutoff: str,
) -> list[str]:
    """Every invariant the archive must not break; empty list means safe."""
    errors: list[str] = []
    original_ids = [str(e.get("event_id")) for e in original]
    retained_ids = [str(e.get("event_id")) for e in retained]
    archived_ids = [str(e.get("event_id")) for e in archived]
    if len(retained) + len(archived) != len(original):
        errors.append("event count changed")
    if set(retained_ids) | set(archived_ids) != set(original_ids):
        errors.append("event id union differs from the original set")
    if set(retained_ids) & set(archived_ids):
        errors.append("an event is both retained and archived")
    if any(not _is_monitor(event) for event in archived):
        errors.append("a non-monitor event was selected for archiving")
    if any(_occurred_day(event) >= cutoff for event in archived):
        errors.append("an in-window event was selected for archiving")
    retained_sequences = _sequences(retained)
    if retained_sequences != sorted(retained_sequences):
        errors.append("retained sequences are not monotonic")
    if len(set(retained_sequences)) != len(retained_sequences):
        errors.append("retained sequences contain duplicates")
    if original and max(retained_sequences or [0]) != max(_sequences(original)):
        # 下一次 append 会从保留集的最大有效 sequence 续号；一旦变小就会复用
        # 已经用过的号。fail closed，交给运维决定（通常意味着账本几乎全是无
        # sequence 的 v1 遗留行）。
        errors.append("archiving would lower the next append sequence")
    if _fold_by_id(retained) != _fold_by_id(original):
        errors.append("monitor projection differs after archiving")
    return errors


def _group_by_month(archived: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for event in archived:
        month = _occurred_day(event)[:7] or "unknown"
        groups.setdefault(month, []).append(event)
    return groups


def _append_jsonl(path: str, events: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, default=str))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _existing_archive_ids(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as handle:
        return {str(json.loads(line).get("event_id")) for line in handle if line.strip()}


def _write_archive(archive_dir: str, archived: list[dict[str, Any]]) -> list[str]:
    written = []
    for month, events in sorted(_group_by_month(archived).items()):
        target = os.path.join(archive_dir, f"{month}.jsonl")
        known = _existing_archive_ids(target)
        missing = [e for e in events if str(e.get("event_id")) not in known]
        if missing:
            _append_jsonl(target, missing)
        written.append(target)
    return written


def _atomic_rewrite(path: str, events: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.archive.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, default=str))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _snapshot(path: str, timestamp: str) -> str | None:
    if not path or not os.path.exists(path):
        return None
    target = f"{path}.pre-archive-{timestamp}"
    shutil.copy2(path, target)
    return target


def _mirror_retained(
    mirror_path: str, retained_ids: set[str], main_ids: set[str]
) -> list[dict[str, Any]]:
    """Mirror keeps what main keeps, plus anything main never had (no data loss)."""
    mirrored = signal_ledger._read_events_unlocked(mirror_path)
    return [
        event
        for event in mirrored
        if str(event.get("event_id")) in retained_ids
        or str(event.get("event_id")) not in main_ids
    ]


def _restore(snapshots: Mapping[str, str | None]) -> list[str]:
    restored = []
    for target, snapshot in snapshots.items():
        if snapshot and os.path.exists(snapshot):
            shutil.copy2(snapshot, target)
            restored.append(target)
    return restored


def _rollback_commands(snapshots: Mapping[str, str | None]) -> list[str]:
    return [
        f"cp {snapshot} {target}"
        for target, snapshot in snapshots.items()
        if snapshot
    ]


def _plan(
    ledger_path: str, cutoff: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    original = signal_ledger._read_events_unlocked(ledger_path)
    archivable = _archivable_ids(original, cutoff)
    retained, archived = _partition(original, archivable)
    return original, retained, archived, _verify(original, retained, archived, cutoff)


def _commit(
    ledger_path: str,
    mirror_path: str | None,
    retained: list[dict[str, Any]],
    archived: list[dict[str, Any]],
    archive_dir: str,
    timestamp: str,
) -> dict[str, Any]:
    snapshots = {ledger_path: _snapshot(ledger_path, timestamp)}
    if mirror_path:
        snapshots[mirror_path] = _snapshot(mirror_path, timestamp)
    outcome: dict[str, Any] = {
        "snapshots": [value for value in snapshots.values() if value],
        "rollback": _rollback_commands(snapshots),
        "rollback_note": (
            "快照是归档那一刻的全量账本；回滚会丢掉归档之后追加的事件。"
            "先停调度器（AUTOPILOT.md）再回滚。"
        ),
    }
    retained_ids = {str(event.get("event_id")) for event in retained}
    main_ids = retained_ids | {str(event.get("event_id")) for event in archived}
    _write_archive(archive_dir, archived)
    _atomic_rewrite(ledger_path, retained)
    if mirror_path and os.path.exists(mirror_path):
        _atomic_rewrite(
            mirror_path, _mirror_retained(mirror_path, retained_ids, main_ids)
        )
    signal_ledger.reset_append_cache()
    outcome["errors"] = _post_verify(ledger_path, archive_dir, retained_ids, main_ids)
    if outcome["errors"]:
        outcome["restored"] = _restore(snapshots)
        signal_ledger.reset_append_cache()
    return outcome


def _post_verify(
    ledger_path: str,
    archive_dir: str,
    retained_ids: set[str],
    main_ids: set[str],
) -> list[str]:
    on_disk = {
        str(event.get("event_id"))
        for event in signal_ledger._read_events_unlocked(ledger_path)
    }
    archived_on_disk: set[str] = set()
    for name in sorted(os.listdir(archive_dir)) if os.path.isdir(archive_dir) else []:
        archived_on_disk |= _existing_archive_ids(os.path.join(archive_dir, name))
    errors = []
    if on_disk != retained_ids:
        errors.append("rewritten ledger does not match the verified plan")
    if not main_ids <= (on_disk | archived_on_disk):
        errors.append("archive files do not cover every removed event")
    return errors


def archive(
    *,
    apply: bool,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    asof: str | None = None,
    signal_ledger_file: str | None = None,
    archive_dir: str | None = None,
) -> dict[str, Any]:
    """Plan (and optionally commit) one archive pass; never writes when unsafe."""
    ledger_path = signal_ledger_file or signal_ledger.LEDGER_FILE
    mirror_path = signal_ledger._ledger_backup_path(ledger_path)
    target_dir = archive_dir or os.path.join(
        os.path.dirname(ledger_path), ARCHIVE_DIR_NAME
    )
    reference = date.fromisoformat(asof) if asof else date.today()
    cutoff = (reference - timedelta(days=max(0, int(retention_days)))).isoformat()
    timestamp = f"{reference.isoformat()}T{os.getpid()}"

    with file_lock(ledger_path):
        original, retained, archived, errors = _plan(ledger_path, cutoff)
        result: dict[str, Any] = {
            "schema": SCHEMA,
            "ok": not errors,
            "apply": apply,
            "dry_run": not apply,
            "signal_ledger": ledger_path,
            "mirror": mirror_path,
            "archive_dir": target_dir,
            "retention_days": int(retention_days),
            "asof": reference.isoformat(),
            "cutoff": cutoff,
            "total_events": len(original),
            "retained_events": len(retained),
            "archived_events": len(archived),
            "non_monitor_events": len([e for e in original if not _is_monitor(e)]),
            "archived_by_month": {
                month: len(events)
                for month, events in sorted(_group_by_month(archived).items())
            },
            "errors": errors,
            "snapshots": [],
            "rollback": [],
        }
        if errors or not apply or not archived:
            return result
        result.update(
            _commit(
                ledger_path, mirror_path, retained, archived, target_dir, timestamp
            )
        )
        result["ok"] = not result["errors"]
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
        "--apply", dest="apply", action="store_true", help="perform the archive"
    )
    parser.set_defaults(apply=False)
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--asof", default=None, help="reference day, defaults to today")
    parser.add_argument("--signal-ledger", default=None)
    parser.add_argument("--archive-dir", default=None)
    args = parser.parse_args()

    result = archive(
        apply=args.apply,
        retention_days=args.retention_days,
        asof=args.asof,
        signal_ledger_file=args.signal_ledger,
        archive_dir=args.archive_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
