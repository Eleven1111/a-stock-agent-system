"""Critical account state uses independent, versioned recovery snapshots."""

from __future__ import annotations

from pathlib import Path

from state_store import atomic_write_json, read_json


def test_missing_critical_state_recovers_from_independent_backup(tmp_path, monkeypatch):
    state = tmp_path / "state"
    backup = tmp_path / "backup"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state))
    monkeypatch.setenv("A_STOCK_BACKUP_HOME", str(backup))
    path = state / "skills" / "stock-triage" / "data" / "portfolio.json"

    atomic_write_json(str(path), {"cash": 100000, "positions": []})
    atomic_write_json(str(path), {"cash": 90000, "positions": [{"code": "600001"}]})
    path.unlink()
    Path(str(path) + ".bak").unlink()

    restored = read_json(str(path), None)

    assert restored == {"cash": 90000, "positions": [{"code": "600001"}]}
    assert path.exists()
    assert list(backup.rglob("*.json"))


def test_noncritical_missing_file_does_not_resurrect(tmp_path, monkeypatch):
    state = tmp_path / "state"
    backup = tmp_path / "backup"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state))
    monkeypatch.setenv("A_STOCK_BACKUP_HOME", str(backup))
    path = state / "skills" / "stock-triage" / "cache" / "temporary.json"

    atomic_write_json(str(path), {"value": 1})
    atomic_write_json(str(path), {"value": 2})
    path.unlink()
    Path(str(path) + ".bak").unlink()

    assert read_json(str(path), None) is None
    assert not list(backup.rglob("*.json"))


def test_first_critical_write_is_immediately_recoverable(tmp_path, monkeypatch):
    state = tmp_path / "state"
    backup = tmp_path / "backup"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state))
    monkeypatch.setenv("A_STOCK_BACKUP_HOME", str(backup))
    path = state / "skills" / "stock-triage" / "data" / "portfolio.json"

    atomic_write_json(str(path), {"cash": 50000, "positions": []})
    path.unlink()

    assert read_json(str(path), None) == {"cash": 50000, "positions": []}


def test_critical_backup_retention_is_bounded(tmp_path, monkeypatch):
    state = tmp_path / "state"
    backup = tmp_path / "backup"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state))
    monkeypatch.setenv("A_STOCK_BACKUP_HOME", str(backup))
    monkeypatch.setenv("A_STOCK_BACKUP_KEEP", "3")
    path = state / "skills" / "stock-triage" / "data" / "portfolio.json"

    for cash in range(10):
        atomic_write_json(str(path), {"cash": cash, "positions": []})

    assert len(list(backup.rglob("*.json"))) == 3
