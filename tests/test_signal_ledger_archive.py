"""signal_ledger 归档：把被后续事件完全覆盖的旧 monitor.* 事件搬出主账本。

不变式（issue #167 任务二）：
- 非 monitor.* 事件一律不归档；
- 保留窗口内的事件一律不归档；
- 归档前后 event_projection.fold_monitor_records 的结果必须逐字段相等 ——
  这是「归档不削弱注册表 fail-closed 校验强度」的证明，不是约定；
- 主账本 + 归档文件的事件并集 == 归档前的全集；
- --dry-run 是默认，不碰任何文件。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

import event_projection
import signal_ledger
import signal_ledger_archive as archiver


ASOF = "2026-08-06"


def _wire(tmp_path, monkeypatch):
    state_home = tmp_path / "state"
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(signal_ledger, "hermes_home", lambda: str(state_home))
    monkeypatch.setattr(signal_ledger, "backup_home", lambda: str(backup_root))
    signal_ledger.reset_append_cache()
    relative = "skills/stock-triage/data/signal_ledger.jsonl"
    return str(state_home / relative), str(backup_root / relative)


def _monitor_event(monitor_id, day, *, status="active", extra=None):
    entry = {"id": monitor_id, "kind": "stock", "key": monitor_id,
             "label": monitor_id, "status": status, "source": "auto"}
    entry.update(extra or {})
    return {
        "event_type": "monitor.activated" if status == "active" else "monitor.deactivated",
        "links": {"monitor_id": monitor_id, "correlation_id": f"corr-{monitor_id}"},
        "payload": {"entry": entry},
        "idempotency_key": f"{monitor_id}:{day}:{status}",
        "occurred_at": f"{day}T09:30:00",
    }


def _recommendation_event(index, day):
    return {
        "event_type": "recommendation.created",
        "links": {"correlation_id": f"corr-rec-{index}"},
        "payload": {"code": "600000"},
        "idempotency_key": f"rec-{index}",
        "occurred_at": f"{day}T09:30:00",
    }


def _seed(path):
    """8 天的 monitor churn + 稀疏 recommendation 事件。"""
    events = []
    for day_offset in range(1, 9):
        day = f"2026-07-{20 + day_offset:02d}"
        for monitor in ("stock:000001", "stock:000002"):
            events.append(_monitor_event(monitor, day))
            events.append(_monitor_event(monitor, day, status="inactive"))
    events.append(_recommendation_event(1, "2026-06-30"))
    events.append(_recommendation_event(2, "2026-07-21"))
    signal_ledger.append_events(events, ledger_file=path)


def _run(path, **kwargs):
    options = {"apply": False, "retention_days": 7, "asof": ASOF,
               "signal_ledger_file": path}
    options.update(kwargs)
    return archiver.archive(**options)


def _fingerprints(*paths):
    return [
        (os.path.getsize(p), os.stat(p).st_mtime_ns) if os.path.exists(p) else None
        for p in paths
    ]


def test_dry_run_is_the_default_and_touches_nothing(tmp_path, monkeypatch):
    path, mirror = _wire(tmp_path, monkeypatch)
    _seed(path)
    before = _fingerprints(path, mirror)

    result = _run(path)

    assert result["dry_run"] is True
    assert result["archived_events"] > 0
    assert _fingerprints(path, mirror) == before
    assert not os.path.exists(result["archive_dir"])


def test_union_of_main_and_archive_equals_the_original_set(tmp_path, monkeypatch):
    path, _ = _wire(tmp_path, monkeypatch)
    _seed(path)
    original = {event["event_id"] for event in signal_ledger.read_events(path)}

    result = _run(path, apply=True)

    retained = {event["event_id"] for event in signal_ledger.read_events(path)}
    archived = set()
    for name in os.listdir(result["archive_dir"]):
        with open(os.path.join(result["archive_dir"], name), encoding="utf-8") as handle:
            archived.update(json.loads(line)["event_id"] for line in handle)
    assert retained | archived == original
    assert retained & archived == set()
    assert len(retained) + len(archived) == len(original)


def test_non_monitor_events_are_never_archived(tmp_path, monkeypatch):
    path, _ = _wire(tmp_path, monkeypatch)
    _seed(path)

    _run(path, apply=True)

    kept = signal_ledger.read_events(path)
    kinds = {event["event_type"] for event in kept}
    assert "recommendation.created" in kinds
    assert len([e for e in kept if e["event_type"] == "recommendation.created"]) == 2


def test_fold_projection_is_bit_identical_after_archive(tmp_path, monkeypatch):
    path, _ = _wire(tmp_path, monkeypatch)
    _seed(path)
    before = event_projection.fold_monitor_records([], signal_ledger.read_events(path))

    _run(path, apply=True)

    after = event_projection.fold_monitor_records([], signal_ledger.read_events(path))
    assert {r["id"]: r for r in after} == {r["id"]: r for r in before}


def test_events_inside_the_retention_window_are_kept(tmp_path, monkeypatch):
    path, _ = _wire(tmp_path, monkeypatch)
    _seed(path)

    result = _run(path, apply=True, retention_days=7)

    cutoff = result["cutoff"]
    assert cutoff == "2026-07-30"
    with open(os.path.join(result["archive_dir"], "2026-07.jsonl"), encoding="utf-8") as handle:
        archived = [json.loads(line) for line in handle]
    assert archived
    assert all(str(event["occurred_at"])[:10] < cutoff for event in archived)


def test_retention_window_can_keep_everything(tmp_path, monkeypatch):
    path, _ = _wire(tmp_path, monkeypatch)
    _seed(path)
    before = signal_ledger.read_events(path)

    result = _run(path, apply=True, retention_days=3650)

    assert result["archived_events"] == 0
    assert signal_ledger.read_events(path) == before


def test_sequence_stays_monotonic_and_appends_continue(tmp_path, monkeypatch):
    path, _ = _wire(tmp_path, monkeypatch)
    _seed(path)
    highest = max(int(e["sequence"]) for e in signal_ledger.read_events(path))

    _run(path, apply=True)

    retained = signal_ledger.read_events(path)
    sequences = [int(event["sequence"]) for event in retained]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert max(sequences) == highest
    appended = signal_ledger.append_events(
        [_monitor_event("stock:000003", "2026-08-06")], ledger_file=path
    )
    assert appended[0]["sequence"] == highest + 1


def test_mirror_is_rewritten_so_restore_cannot_reinject(tmp_path, monkeypatch):
    path, mirror = _wire(tmp_path, monkeypatch)
    _seed(path)

    _run(path, apply=True)

    retained = {event["event_id"] for event in signal_ledger.read_events(path)}
    mirrored = {event["event_id"] for event in signal_ledger.read_events(mirror)}
    assert mirrored == retained
    os.unlink(path)
    assert {e["event_id"] for e in signal_ledger.read_events(path)} == retained


def test_second_run_is_a_noop(tmp_path, monkeypatch):
    path, _ = _wire(tmp_path, monkeypatch)
    _seed(path)
    _run(path, apply=True)
    after_first = _fingerprints(path)

    second = _run(path, apply=True)

    assert second["archived_events"] == 0
    assert _fingerprints(path) == after_first


def test_snapshots_and_rollback_commands_are_reported(tmp_path, monkeypatch):
    path, mirror = _wire(tmp_path, monkeypatch)
    _seed(path)
    original = signal_ledger.read_events(path)

    result = _run(path, apply=True)

    assert result["snapshots"]
    assert result["rollback"]
    for command in result["rollback"]:
        assert command.startswith("cp ")
    snapshot = next(s for s in result["snapshots"] if s.startswith(path))
    with open(snapshot, encoding="utf-8") as handle:
        assert [json.loads(line)["event_id"] for line in handle] == [
            event["event_id"] for event in original
        ]
    assert mirror


def test_verification_failure_aborts_before_touching_files(tmp_path, monkeypatch):
    path, mirror = _wire(tmp_path, monkeypatch)
    _seed(path)
    before = _fingerprints(path, mirror)

    original_select = archiver._archivable_ids

    def _lossy(events, cutoff):
        # 故意多归档：把一条不该动的 recommendation 事件也算进去。
        ids = original_select(events, cutoff)
        extra = next(
            event["event_id"]
            for event in events
            if event["event_type"] == "recommendation.created"
        )
        return ids | {extra}

    monkeypatch.setattr(archiver, "_archivable_ids", _lossy)
    result = _run(path, apply=True)

    assert result["ok"] is False
    assert result["errors"]
    assert _fingerprints(path, mirror) == before


def test_verification_catches_a_projection_regression(tmp_path, monkeypatch):
    path, mirror = _wire(tmp_path, monkeypatch)
    _seed(path)
    before = _fingerprints(path, mirror)

    def _too_greedy(events, cutoff):
        return {
            event["event_id"]
            for event in events
            if str(event["event_type"]).startswith("monitor.")
        }

    monkeypatch.setattr(archiver, "_archivable_ids", _too_greedy)
    result = _run(path, apply=True)

    assert result["ok"] is False
    assert _fingerprints(path, mirror) == before


def test_legacy_rows_without_sequence_are_handled_in_the_same_units(tmp_path, monkeypatch):
    """生产账本前 5319 行是无 sequence 的 v1 遗留行，归档会移动它们的行号。"""
    path, _ = _wire(tmp_path, monkeypatch)
    legacy = []
    for index, day in enumerate(("2026-07-21", "2026-07-22", "2026-07-23"), start=1):
        event = signal_ledger._normalize_event(_monitor_event("stock:000001", day))
        event["schema"] = "signal_ledger_event_v1"
        event.pop("sequence", None)
        legacy.append(event)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for event in legacy:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    signal_ledger.reset_append_cache()

    result = _run(path, apply=True)

    # 全遗留行的账本一旦归档就会让下一次 append 的续号变小 —— 必须 fail closed。
    assert result["ok"] is False
    assert "archiving would lower the next append sequence" in result["errors"]


def test_post_write_failure_restores_the_snapshots(tmp_path, monkeypatch):
    """写完才发现归档文件没覆盖全部搬走的事件时，必须回滚到快照。"""
    path, mirror = _wire(tmp_path, monkeypatch)
    _seed(path)
    before = [event["event_id"] for event in signal_ledger.read_events(path)]

    monkeypatch.setattr(archiver, "_write_archive", lambda directory, events: [])
    result = _run(path, apply=True)

    assert result["ok"] is False
    assert result["restored"] == [path, mirror]
    assert [e["event_id"] for e in signal_ledger.read_events(path)] == before
    assert [e["event_id"] for e in signal_ledger.read_events(mirror)] == before


def test_cli_defaults_to_dry_run(tmp_path, monkeypatch, capsys):
    path, mirror = _wire(tmp_path, monkeypatch)
    _seed(path)
    before = _fingerprints(path, mirror)
    monkeypatch.setattr(
        sys, "argv",
        ["signal_ledger_archive.py", "--signal-ledger", path, "--asof", ASOF],
    )

    assert archiver.main() == 0

    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert _fingerprints(path, mirror) == before


def test_registry_fail_closed_check_survives_archive(tmp_path, monkeypatch):
    """归档后注册表校验既要仍然通过，也要仍然能抓到被篡改的注册表。"""
    import monitor_registry as registry

    path = str(tmp_path / "signal_ledger.jsonl")
    monkeypatch.setattr(registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(registry, "LEDGER_FILE", path)
    monkeypatch.setattr(registry, "MIRROR_LEDGER_FILE", str(tmp_path / "monitor_ledger.jsonl"))
    monkeypatch.setattr(registry, "CHECKPOINT_FILE", str(tmp_path / "checkpoint.json"))
    registry.reset_verification_cache()
    signal_ledger.reset_append_cache()
    for _ in range(3):
        registry.activate("stock", "000001", "平安银行", source="auto")
        registry.cancel("stock", "000001", reason="rotate", manual=False, status="inactive")
    registry.activate("stock", "000002", "万科A", source="auto")

    _run(path, apply=True, retention_days=7, asof="2026-12-31")
    registry.reset_verification_cache()

    assert {item["id"] for item in registry.load_registry()} == {
        "stock:000001", "stock:000002",
    }
    with open(str(tmp_path / "monitor_registry.json"), "w", encoding="utf-8") as handle:
        json.dump([], handle)
    registry.reset_verification_cache()
    with pytest.raises(RuntimeError, match="projection mismatch"):
        registry.load_registry()
