"""append_events 热路径缓存：同进程连续追加不再全量重读账本。

背景（issue #167）：一次 append 会全量读 14MB 账本三次（自身去重、备份对账
读主账本、读备份账本），生产上单次 0.39~0.47s，500 只候选逐个 activate 要
四分钟。缓存必须能自愈：两次 append 之间别的进程可能追加，靠文件指纹发现。
"""

from __future__ import annotations

import json

import pytest

import signal_ledger as ledger


def _state(tmp_path, monkeypatch):
    state_home = tmp_path / "state"
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(ledger, "hermes_home", lambda: str(state_home))
    monkeypatch.setattr(ledger, "backup_home", lambda: str(backup_root))
    relative = "skills/stock-triage/data/signal_ledger.jsonl"
    return state_home / relative, backup_root / relative


def _monitor_event(key):
    return {
        "event_type": "monitor.activated",
        "links": {"monitor_id": key, "correlation_id": "corr-bench"},
        "payload": {"entry": {"id": key, "status": "active"}},
        "idempotency_key": key,
    }


def _count_reads(monkeypatch):
    calls = []
    original = ledger._read_events_unlocked

    def _counting(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(ledger, "_read_events_unlocked", _counting)
    return calls


@pytest.fixture(autouse=True)
def _reset_cache():
    ledger.reset_append_cache()
    yield
    ledger.reset_append_cache()


def test_warm_appends_never_reread_the_ledger(tmp_path, monkeypatch):
    path, _ = _state(tmp_path, monkeypatch)
    ledger.append_events([_monitor_event("warmup")], ledger_file=str(path))

    calls = _count_reads(monkeypatch)
    for index in range(10):
        ledger.append_events([_monitor_event(f"m{index}")], ledger_file=str(path))

    assert calls == []
    assert len(ledger.read_events(str(path))) == 11


def test_first_append_reads_each_file_at_most_once(tmp_path, monkeypatch):
    path, backup = _state(tmp_path, monkeypatch)
    ledger.append_events([_monitor_event("seed")], ledger_file=str(path))
    ledger.reset_append_cache()

    calls = _count_reads(monkeypatch)
    ledger.append_events([_monitor_event("cold")], ledger_file=str(path))

    assert calls.count(str(path)) == 1
    assert calls.count(str(backup)) <= 1


def test_external_process_append_invalidates_cache(tmp_path, monkeypatch):
    path, _ = _state(tmp_path, monkeypatch)
    ledger.append_events([_monitor_event("first")], ledger_file=str(path))

    foreign = {
        "schema": ledger.SCHEMA,
        "event_id": "evt-foreign",
        "event_type": "monitor.activated",
        "occurred_at": "2026-08-01T09:00:00",
        "links": {"correlation_id": "corr-foreign", "monitor_id": "foreign"},
        "payload": {"entry": {"id": "foreign", "status": "active"}},
        "sequence": 2,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(foreign, ensure_ascii=False) + "\n")

    appended = ledger.append_events([_monitor_event("third")], ledger_file=str(path))

    assert [event["sequence"] for event in appended] == [3]
    events = ledger.read_events(str(path))
    assert [event["sequence"] for event in events] == [1, 2, 3]


def test_external_duplicate_is_still_deduplicated(tmp_path, monkeypatch):
    path, _ = _state(tmp_path, monkeypatch)
    ledger.append_events([_monitor_event("first")], ledger_file=str(path))
    duplicate = ledger._normalize_event(_monitor_event("outside"))
    duplicate["sequence"] = 2
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(duplicate, ensure_ascii=False) + "\n")

    appended = ledger.append_events([_monitor_event("outside")], ledger_file=str(path))

    assert appended == []
    assert len(ledger.read_events(str(path))) == 2


def test_sequences_stay_dense_and_unique(tmp_path, monkeypatch):
    path, _ = _state(tmp_path, monkeypatch)
    for index in range(50):
        ledger.append_events([_monitor_event(f"s{index}")], ledger_file=str(path))

    sequences = [event["sequence"] for event in ledger.read_events(str(path))]

    assert sequences == list(range(1, 51))


def test_backup_mirror_tracks_cached_appends(tmp_path, monkeypatch):
    path, backup = _state(tmp_path, monkeypatch)
    for index in range(6):
        ledger.append_events([_monitor_event(f"b{index}")], ledger_file=str(path))

    mirrored = ledger.read_events(str(backup))

    assert [event["event_id"] for event in mirrored] == [
        event["event_id"] for event in ledger.read_events(str(path))
    ]


def test_externally_truncated_backup_is_fully_reconciled(tmp_path, monkeypatch):
    path, backup = _state(tmp_path, monkeypatch)
    for index in range(4):
        ledger.append_events([_monitor_event(f"t{index}")], ledger_file=str(path))
    backup.write_text("", encoding="utf-8")

    ledger.append_events([_monitor_event("t4")], ledger_file=str(path))

    assert [event["event_id"] for event in ledger.read_events(str(backup))] == [
        event["event_id"] for event in ledger.read_events(str(path))
    ]


def _tail_close_event(fill_price):
    from tail_close_test_support import TRADING_DATE

    record = {
        "strategy_id": "tail_close_v1",
        "signal_date": TRADING_DATE,
        "signal_id": "sig-tail-1",
        "code": "600000",
        "fill_price": fill_price,
        "provenance": {
            "decision_mode": "live",
            "snapshot_id": "snap-1",
            "snapshot_hash": "a" * 64,
            "config_hash": "b" * 64,
            "code_version": "v1",
        },
    }
    links = {"correlation_id": "corr-tail-1", "signal_id": "sig-tail-1"}
    return ledger.research_signal_event(record, links)


def test_tail_close_idempotency_conflict_still_raises(tmp_path, monkeypatch):
    path, _ = _state(tmp_path, monkeypatch)
    ledger.append_events([_tail_close_event(10.0)], ledger_file=str(path))

    with pytest.raises(ValueError, match="tail-close idempotency conflict"):
        ledger.append_events([_tail_close_event(11.0)], ledger_file=str(path))


def test_tail_close_replay_of_identical_fact_is_a_noop(tmp_path, monkeypatch):
    path, _ = _state(tmp_path, monkeypatch)
    ledger.append_events([_tail_close_event(10.0)], ledger_file=str(path))

    assert ledger.append_events([_tail_close_event(10.0)], ledger_file=str(path)) == []
    assert len(ledger.read_events(str(path))) == 1


def test_tail_close_conflict_detected_after_warm_monitor_appends(tmp_path, monkeypatch):
    """缓存只保留 tail_close 事实时，热路径仍必须能识别冲突。"""
    path, _ = _state(tmp_path, monkeypatch)
    ledger.append_events([_tail_close_event(10.0)], ledger_file=str(path))
    for index in range(5):
        ledger.append_events([_monitor_event(f"w{index}")], ledger_file=str(path))

    with pytest.raises(ValueError, match="tail-close idempotency conflict"):
        ledger.append_events([_tail_close_event(12.5)], ledger_file=str(path))


def test_rewritten_ledger_with_same_size_is_detected(tmp_path, monkeypatch):
    """归档/迁移用 os.replace 重写账本；长度可能不变，指纹必须覆盖 inode。"""
    path, _ = _state(tmp_path, monkeypatch)
    ledger.append_events([_monitor_event("r0")], ledger_file=str(path))
    ledger.append_events([_monitor_event("r1")], ledger_file=str(path))
    events = ledger.read_events(str(path))

    replacement = str(path) + ".rewrite.tmp"
    with open(replacement, "w", encoding="utf-8") as handle:
        for event in events[:1]:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    import os

    os.replace(replacement, path)

    ledger.append_events([_monitor_event("r2")], ledger_file=str(path))
    sequences = [event["sequence"] for event in ledger.read_events(str(path))]

    assert sequences == [1, 2]
