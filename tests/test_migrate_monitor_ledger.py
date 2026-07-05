"""Migration of monitor.* events out of the signal ledger."""

import glob
import importlib.util
import json
import os
from pathlib import Path

import monitor_ledger
import signal_ledger

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_monitor_ledger.py"
SPEC = importlib.util.spec_from_file_location("migrate_monitor_ledger", SCRIPT)
migrate_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migrate_mod)


def _write_jsonl(path, events):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False))
            handle.write("\n")


def _signal_event(event_id):
    return {
        "schema": "signal_ledger_event_v2",
        "event_id": event_id,
        "event_type": "signal.opened",
        "occurred_at": "2026-07-05T09:30:00",
        "links": {"correlation_id": "corr-" + event_id, "signal_id": "sig-" + event_id},
        "payload": {"code": "600011"},
    }


def _monitor_event(event_id, event_type="monitor.activated"):
    return {
        "schema": "signal_ledger_event_v2",
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": "2026-07-05T09:30:00",
        "links": {"correlation_id": "corr-" + event_id, "monitor_id": "stock:600011"},
        "payload": {"kind": "stock", "key": "600011"},
    }


def _state_home(tmp_path, monkeypatch):
    state = tmp_path / "state"
    backup = tmp_path / "backup"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state))
    monkeypatch.setenv("A_STOCK_BACKUP_HOME", str(backup))
    # signal_ledger derives paths at import time from module constants;
    # re-point them at the fresh state home for this test.
    ledger_path = signal_ledger.data_file("stock-triage", "signal_ledger.jsonl")
    monitor_path = monitor_ledger.data_file("stock-triage", "monitor_ledger.jsonl")
    return ledger_path, monitor_path


def test_dry_run_does_not_touch_files(tmp_path, monkeypatch):
    ledger_path, monitor_path = _state_home(tmp_path, monkeypatch)
    _write_jsonl(ledger_path, [_signal_event("s1"), _monitor_event("m1")])
    mirror_path = signal_ledger._ledger_backup_path(ledger_path)
    _write_jsonl(mirror_path, [_signal_event("s1"), _monitor_event("m1")])

    ledger_before = Path(ledger_path).read_text(encoding="utf-8")
    mirror_before = Path(mirror_path).read_text(encoding="utf-8")

    result = migrate_mod.migrate(
        apply=False,
        signal_ledger_file=ledger_path,
        monitor_ledger_file=monitor_path,
    )

    assert result["dry_run"] is True
    assert result["migrated_events"] == 1
    assert result["kept_events"] == 1
    assert result["mirror_migrated_events"] == 1
    # Nothing mutated.
    assert Path(ledger_path).read_text(encoding="utf-8") == ledger_before
    assert Path(mirror_path).read_text(encoding="utf-8") == mirror_before
    assert not os.path.exists(monitor_path)
    assert glob.glob(str(Path(ledger_path).parent / "*.pre-migration-*")) == []


def test_apply_migrates_main_and_mirror(tmp_path, monkeypatch):
    ledger_path, monitor_path = _state_home(tmp_path, monkeypatch)
    _write_jsonl(
        ledger_path,
        [
            _signal_event("s1"),
            _monitor_event("m1", "monitor.activated"),
            _signal_event("s2"),
            _monitor_event("m2", "monitor.deactivated"),
        ],
    )
    mirror_path = signal_ledger._ledger_backup_path(ledger_path)
    _write_jsonl(
        mirror_path,
        [
            _signal_event("s1"),
            _monitor_event("m1", "monitor.activated"),
            _monitor_event("m3", "monitor.cancelled"),
        ],
    )

    result = migrate_mod.migrate(
        apply=True,
        signal_ledger_file=ledger_path,
        monitor_ledger_file=monitor_path,
    )

    assert result["apply"] is True
    assert result["noop"] is False
    assert result["migrated_events"] == 2
    assert result["kept_events"] == 2

    # Main signal ledger keeps only signal events.
    main_events = signal_ledger.read_events(ledger_path)
    assert {event["event_type"] for event in main_events} == {"signal.opened"}
    assert len(main_events) == 2

    # Mirror cleaned too — no monitor.* left to re-inject on future restore.
    mirror_events = signal_ledger._read_events_unlocked(mirror_path)
    assert all(
        not event["event_type"].startswith("monitor.") for event in mirror_events
    )

    # Monitor ledger received the migrated events (main + mirror-only ones).
    monitor_events = monitor_ledger.read_events(monitor_path)
    migrated_types = sorted(event["event_type"] for event in monitor_events)
    assert migrated_types == [
        "monitor.activated",
        "monitor.cancelled",
        "monitor.deactivated",
    ]

    # Pre-migration snapshots exist for both files.
    snapshots = glob.glob(str(Path(ledger_path) ) + ".pre-migration-*")
    assert snapshots
    mirror_snapshots = glob.glob(str(Path(mirror_path)) + ".pre-migration-*")
    assert mirror_snapshots


def test_apply_is_noop_when_no_monitor_events(tmp_path, monkeypatch):
    ledger_path, monitor_path = _state_home(tmp_path, monkeypatch)
    _write_jsonl(ledger_path, [_signal_event("s1")])

    result = migrate_mod.migrate(
        apply=True,
        signal_ledger_file=ledger_path,
        monitor_ledger_file=monitor_path,
    )

    assert result["noop"] is True
    assert result["migrated_events"] == 0
    # Untouched: no snapshot, no monitor ledger.
    assert glob.glob(str(Path(ledger_path)) + ".pre-migration-*") == []
    assert not os.path.exists(monitor_path)
