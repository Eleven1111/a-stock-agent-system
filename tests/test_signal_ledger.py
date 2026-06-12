"""Append-only signal ledger tests."""

import json
import threading

import signal_ledger as ledger


def test_append_is_idempotent_and_projects_settlement(tmp_path):
    path = str(tmp_path / "signal_ledger.jsonl")
    links = ledger.make_links("rec-1", include_trade=True, monitor_id="stock:002156")
    opened = ledger.signal_opened_event(
        {
            "code": "002156",
            "name": "通富微电",
            "date": "2026-06-12",
            "entry_price": 11.0,
            "grade": "A",
            "strategy_id": "daban:first_board_reseal",
            "action": "buy",
        },
        links,
    )
    settled = ledger.settlement_event(
        {**opened["payload"], **links},
        {"outcome": "win", "t1_close_ret": 6.5},
    )

    ledger.append_events([opened, opened, settled], ledger_file=path)

    events = ledger.read_events(path)
    records = ledger.project_signals(events)
    assert len(events) == 2
    assert len(records) == 1
    assert records[0]["outcome"] == "win"
    assert records[0]["t1_close_ret"] == 6.5
    assert records[0]["recommendation_id"] == "rec-1"
    assert records[0]["trade_id"] == links["trade_id"]
    assert records[0]["monitor_id"] == "stock:002156"
    assert records[0]["settlement_id"]


def test_projection_folds_provisional_then_final_settlement(tmp_path):
    path = str(tmp_path / "signal_ledger.jsonl")
    links = ledger.make_links("rec-stage")
    opened = ledger.signal_opened_event(
        {
            "code": "002156",
            "name": "通富微电",
            "date": "2026-06-10",
            "entry_price": 10.0,
            "action": "buy",
        },
        links,
    )
    t1 = ledger.settlement_event(
        {**opened["payload"], **links},
        {
            "outcome": "win",
            "t1_close_ret": 3.0,
            "settlement_status": "provisional",
            "resolved": False,
        },
        stage="t1",
    )
    t3 = ledger.settlement_event(
        {**opened["payload"], **links},
        {
            "outcome": "win",
            "t1_close_ret": 3.0,
            "horizon_ret": 8.0,
            "settlement_status": "final",
            "resolved": True,
        },
        stage="t3",
    )

    ledger.append_events([opened, t1, t3], ledger_file=path)

    record = ledger.project_signals(ledger_file=path)[0]
    assert record["settlement_status"] == "final"
    assert record["horizon_ret"] == 8.0
    assert [event["event_type"] for event in ledger.read_events(path)] == [
        "signal.opened",
        "signal.t1_settled",
        "signal.t3_settled",
    ]


def test_concurrent_append_keeps_every_event(tmp_path):
    path = str(tmp_path / "signal_ledger.jsonl")

    def write(index):
        links = ledger.make_links(f"rec-{index}")
        ledger.append_event(
            "recommendation.created",
            links,
            {"index": index},
            idempotency_key=f"rec-{index}",
            ledger_file=path,
        )

    threads = [threading.Thread(target=write, args=(index,)) for index in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = (tmp_path / "signal_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 30
    assert all(json.loads(line)["schema"] == ledger.SCHEMA for line in lines)


def test_merge_legacy_assigns_stable_ids_and_deduplicates():
    legacy = {
        "code": "002156",
        "signal_date": "2026-06-12",
        "strategy_id": "trend_pullback",
        "grade": "A",
        "outcome": "pending",
    }
    first = ledger.merge_legacy_signals([], [legacy])
    second = ledger.merge_legacy_signals(first, [legacy])
    assert len(second) == 1
    assert first[0]["signal_id"] == second[0]["signal_id"]
