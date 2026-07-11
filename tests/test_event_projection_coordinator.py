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
