import pytest

import json

import event_projection


def _append_jsonl(path, event):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_projection_failure_keeps_event_and_does_not_advance_checkpoint(tmp_path):
    ledger = tmp_path / "events.jsonl"
    checkpoint = tmp_path / "checkpoint.json"
    applied = []

    def append(event):
        _append_jsonl(ledger, event)

    def first(event):
        applied.append(("first", event["sequence"]))

    def broken(event):
        raise RuntimeError("projection failed")

    result = event_projection.append_project_checkpoint(
        {"sequence": 1, "event_id": "e1", "event_type": "cash.deposited"},
        append_event=append,
        projectors=[first, broken],
        checkpoint_file=str(checkpoint),
    )
    assert result["status"] == "replay_required"
    assert _read_jsonl(ledger)[0]["event_id"] == "e1"
    assert not checkpoint.exists()


def test_restart_replays_all_projections_before_monotonic_checkpoint(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    events = [
        {"sequence": 1, "event_id": "e1", "event_type": "position.opened"},
        {"sequence": 2, "event_id": "e2", "event_type": "trade.executed"},
    ]
    seen_a, seen_b = [], []
    result = event_projection.replay_events(
        events,
        projectors=[lambda e: seen_a.append(e["event_id"]), lambda e: seen_b.append(e["event_id"])],
        checkpoint_file=str(checkpoint),
    )
    assert result["status"] == "ok"
    assert json.loads(checkpoint.read_text())["sequence"] == 2
    assert seen_a == seen_b == ["e1", "e2"]
    replayed = event_projection.replay_events(
        events,
        projectors=[lambda e: seen_a.append(e["event_id"])],
        checkpoint_file=str(checkpoint),
    )
    assert replayed["applied"] == 0


def test_reconciliation_blocks_cash_position_trade_or_monitor_mismatch():
    expected = {
        "cash": 100.0,
        "positions": {"600001": 100},
        "trades": {"t1"},
        "monitors": {"stock:600001"},
    }
    for field in expected:
        actual = {**expected, field: object()}
        result = event_projection.reconcile_projections(expected, actual)
        assert result["status"] == "projection_mismatch"
        assert field in result["mismatches"]
        assert result["allow_new_risk"] is False


def _monitor_event(monitor_id, status, sequence):
    return {
        "event_id": f"evt-{sequence}",
        "sequence": sequence,
        "event_type": "monitor.activated" if status == "active" else "monitor.deactivated",
        "links": {"monitor_id": monitor_id},
        "payload": {"entry": {"id": monitor_id, "status": status, "key": monitor_id}},
    }


def test_fold_monitor_records_matches_the_incremental_fold():
    """批量折叠必须与逐条 project_monitor_records 逐字段等价。

    校验路径原本对每条事件都深拷贝整个 records 列表（O(事件×记录)），
    12078 条事件 × 2029 条记录实测 4.8s，是竞价链超时的直接来源（issue #167）。
    批量折叠改用 id 索引，但结果必须与旧实现完全一致，否则 fail-closed 校验
    会开始误报或漏报。
    """
    events = []
    for index in range(1, 61):
        monitor_id = f"m{index % 7}"
        status = "active" if index % 3 else "inactive"
        events.append(_monitor_event(monitor_id, status, index))
    events.append({"event_id": "evt-x", "sequence": 61, "event_type": "paper.daily_nav", "payload": {}})

    incremental = []
    for event in events:
        incremental = event_projection.project_monitor_records(incremental, event)

    assert event_projection.fold_monitor_records([], events) == incremental


def test_fold_monitor_records_keeps_a_non_empty_starting_projection():
    seed = [{"id": "m0", "status": "active", "key": "m0", "extra": "kept"}]
    events = [_monitor_event("m1", "active", 1), _monitor_event("m0", "inactive", 2)]

    folded = event_projection.fold_monitor_records(seed, events)

    assert [item["id"] for item in folded] == ["m0", "m1"]
    assert folded[0]["status"] == "inactive"
    assert folded[0]["extra"] == "kept"       # 合并而非替换
    assert seed[0]["status"] == "active"      # 入参不被就地修改


def test_fold_monitor_records_still_rejects_an_event_without_monitor_id():
    with pytest.raises(ValueError):
        event_projection.fold_monitor_records(
            [], [{"event_id": "e", "sequence": 1, "event_type": "monitor.activated", "payload": {}}]
        )


def test_batch_projector_replays_once_and_advances_to_the_last_event(tmp_path):
    """冷重放此前是每个事件一次落盘写。

    monitor 投影每条事件都 mutate_json 整份注册表（2029 条），于是从零
    checkpoint 重放 12000 条要 O(事件 × 记录) 次文件读写 —— 实测 81s。
    批投影把折叠放进内存，只写一次。
    """
    checkpoint = tmp_path / "checkpoint.json"
    events = [
        {"sequence": seq, "event_id": f"e{seq}", "event_type": "monitor.activated"}
        for seq in range(1, 6)
    ]
    batches = []

    result = event_projection.replay_events(
        events,
        projectors=[],
        checkpoint_file=str(checkpoint),
        batch_projector=lambda pending: batches.append([e["event_id"] for e in pending]),
    )

    assert result["status"] == "ok"
    assert result["applied"] == 5
    assert batches == [["e1", "e2", "e3", "e4", "e5"]]
    assert json.loads(checkpoint.read_text())["sequence"] == 5

    # 追平之后再来一次：既不该重放，也不该再写 checkpoint。
    again = event_projection.replay_events(
        events,
        projectors=[],
        checkpoint_file=str(checkpoint),
        batch_projector=lambda pending: batches.append("must not run"),
    )
    assert again["applied"] == 0
    assert len(batches) == 1


def test_batch_projector_only_sees_events_after_the_checkpoint(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps({"schema": "projection_checkpoint_v1", "sequence": 3, "event_id": "e3"}),
        encoding="utf-8",
    )
    events = [
        {"sequence": seq, "event_id": f"e{seq}", "event_type": "monitor.activated"}
        for seq in range(1, 6)
    ]
    batches = []

    event_projection.replay_events(
        events,
        projectors=[],
        checkpoint_file=str(checkpoint),
        batch_projector=lambda pending: batches.append([e["event_id"] for e in pending]),
    )

    assert batches == [["e4", "e5"]]


def test_batch_projector_failure_leaves_the_checkpoint_untouched(tmp_path):
    """批写失败必须整批不推进 —— 下次从原点重放，投影是幂等的。"""
    checkpoint = tmp_path / "checkpoint.json"
    events = [
        {"sequence": seq, "event_id": f"e{seq}", "event_type": "monitor.activated"}
        for seq in range(1, 4)
    ]

    def _boom(pending):
        raise RuntimeError("registry write failed")

    result = event_projection.replay_events(
        events,
        projectors=[],
        checkpoint_file=str(checkpoint),
        batch_projector=_boom,
    )

    assert result["status"] == "replay_required"
    assert result["allow_new_risk"] is False
    assert result["failed_sequence"] == 1
    assert not checkpoint.exists()
