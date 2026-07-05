"""Append-only monitor lifecycle ledger tests."""

import monitor_ledger


def test_append_and_read_roundtrip(tmp_path):
    path = str(tmp_path / "monitor_ledger.jsonl")

    first = monitor_ledger.append_event(
        "monitor.activated",
        {"monitor_id": "stock:600011", "correlation_id": "corr-1"},
        {"kind": "stock", "key": "600011", "status": "active"},
        occurred_at="2026-07-05T09:30:00",
        ledger_file=path,
    )
    monitor_ledger.append_event(
        "monitor.deactivated",
        {"monitor_id": "stock:600011"},
        {"kind": "stock", "key": "600011", "status": "inactive"},
        ledger_file=path,
    )

    events = monitor_ledger.read_events(path)
    assert [event["event_type"] for event in events] == [
        "monitor.activated",
        "monitor.deactivated",
    ]
    assert first["schema"] == "monitor_ledger_event_v1"
    assert first["occurred_at"] == "2026-07-05T09:30:00"
    assert events[0]["links"]["monitor_id"] == "stock:600011"
    assert events[0]["payload"]["status"] == "active"


def test_append_is_pure_append_not_deduped(tmp_path):
    path = str(tmp_path / "monitor_ledger.jsonl")
    for _ in range(3):
        monitor_ledger.append_event(
            "monitor.activated",
            {"monitor_id": "stock:600011"},
            {"key": "600011"},
            occurred_at="2026-07-05T09:30:00",
            ledger_file=path,
        )
    # No full-file dedup scan: identical events all land.
    assert len(monitor_ledger.read_events(path)) == 3


def test_read_tolerates_corrupt_lines(tmp_path):
    path = str(tmp_path / "monitor_ledger.jsonl")
    monitor_ledger.append_event(
        "monitor.activated",
        {"monitor_id": "stock:600011"},
        {"key": "600011"},
        ledger_file=path,
    )
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{not valid json\n")
        handle.write("\n")
        handle.write('{"schema": "other_schema", "event_type": "x"}\n')
    monitor_ledger.append_event(
        "monitor.deactivated",
        {"monitor_id": "stock:600011"},
        {"key": "600011"},
        ledger_file=path,
    )

    events = monitor_ledger.read_events(path)
    assert [event["event_type"] for event in events] == [
        "monitor.activated",
        "monitor.deactivated",
    ]


def test_read_missing_file_returns_empty(tmp_path):
    assert monitor_ledger.read_events(str(tmp_path / "missing.jsonl")) == []


def test_append_events_batch(tmp_path):
    path = str(tmp_path / "monitor_ledger.jsonl")
    appended = monitor_ledger.append_events(
        [
            {"event_type": "monitor.activated", "links": {"monitor_id": "a"}},
            {"event_type": "monitor.deactivated", "links": {"monitor_id": "a"}},
        ],
        ledger_file=path,
    )
    assert len(appended) == 2
    assert len(monitor_ledger.read_events(path)) == 2
