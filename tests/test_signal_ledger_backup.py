"""The canonical append-only ledger is mirrored outside the live state root."""

from __future__ import annotations

import signal_ledger


def test_deleted_primary_ledger_recovers_from_mirror(tmp_path, monkeypatch):
    state = tmp_path / "state"
    backup = tmp_path / "backup"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state))
    monkeypatch.setenv("A_STOCK_BACKUP_HOME", str(backup))
    ledger = state / "skills" / "stock-triage" / "data" / "signal_ledger.jsonl"
    links = signal_ledger.make_links(signal_id="signal-1")

    signal_ledger.append_event(
        "signal.opened",
        links,
        {"code": "600001"},
        idempotency_key="signal-1",
        ledger_file=str(ledger),
    )
    ledger.unlink()

    events = signal_ledger.read_events(str(ledger))

    assert len(events) == 1
    assert events[0]["payload"]["code"] == "600001"
    assert ledger.exists()
    assert list(backup.rglob("signal_ledger.jsonl"))
