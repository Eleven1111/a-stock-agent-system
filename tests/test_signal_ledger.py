"""Append-only signal ledger tests."""

import json
import threading

import pytest

import signal_ledger as ledger


def _ledger_line(event_id, *, event_type="recommendation.created"):
    return json.dumps(
        {
            "schema": ledger.SCHEMA,
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": "2026-07-10T09:35:00",
            "links": {"correlation_id": f"corr-{event_id}"},
            "payload": {},
        },
        ensure_ascii=False,
    )


def _backup_paths(tmp_path, monkeypatch):
    state_home = tmp_path / "state"
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(ledger, "hermes_home", lambda: str(state_home))
    monkeypatch.setattr(ledger, "backup_home", lambda: str(backup_root))
    relative = "skills/stock-triage/data/signal_ledger.jsonl"
    return state_home / relative, backup_root / relative


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


def test_appended_events_receive_monotonic_sequences(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    links = ledger.make_links(signal_id="s1", correlation_id="c1")
    first = ledger.append_event(
        "signal.opened", links, {"code": "600001"}, ledger_file=path
    )
    duplicate = ledger.append_event(
        "signal.opened",
        links,
        {"code": "600001"},
        idempotency_key="same",
        ledger_file=path,
    )
    third = ledger.append_event(
        "signal.settled",
        links,
        {"code": "600001"},
        idempotency_key="settled",
        ledger_file=path,
    )
    assert first["sequence"] == 1
    assert duplicate["sequence"] == 2
    assert third["sequence"] == 3


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


def test_read_legacy_recommendation_event_defaults_unknown_evidence_source(tmp_path):
    path = tmp_path / "signal_ledger.jsonl"
    links = ledger.make_links("rec-legacy")
    path.write_text(
        json.dumps(
            {
                "schema": "signal_ledger_event_v1",
                "event_id": "evt-legacy",
                "event_type": "recommendation.created",
                "occurred_at": "2026-06-12T09:35:00",
                "links": links,
                "payload": {
                    "id": "rec-legacy",
                    "code": "002156",
                    "action": "buy",
                },
            },
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )

    event = ledger.read_events(str(path))[0]

    assert event["payload"]["evidence_sources"] == [
        {"source": "unknown", "artifact": "unknown", "weight_hint": "context"}
    ]


def test_read_fails_closed_on_corrupt_middle_line_without_rewriting_source(tmp_path):
    path = tmp_path / "signal_ledger.jsonl"
    original = (
        _ledger_line("evt-before")
        + "\n"
        + '{"schema":"signal_ledger_event_v2","payload":'
        + "\n"
        + _ledger_line("evt-after")
        + "\n"
    ).encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(ledger.SignalLedgerCorruptionError, match="line 2"):
        ledger.read_events(str(path))

    assert path.read_bytes() == original


def test_projection_fails_closed_on_truncated_tail_without_rewriting_source(tmp_path):
    path = tmp_path / "signal_ledger.jsonl"
    original = (
        _ledger_line("evt-complete") + "\n" + '{"schema":"signal_ledger_event_v2"'
    ).encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(ledger.SignalLedgerCorruptionError, match="line 2"):
        ledger.project_signals(ledger_file=str(path))

    assert path.read_bytes() == original


def test_backup_sync_fails_closed_when_primary_is_corrupt_and_backup_exists(
    tmp_path,
    monkeypatch,
):
    path, backup = _backup_paths(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    backup.parent.mkdir(parents=True)
    primary_original = (
        _ledger_line("evt-before") + "\n" + "not-json\n" + _ledger_line("evt-after") + "\n"
    ).encode("utf-8")
    backup_original = (_ledger_line("evt-before") + "\n").encode("utf-8")
    path.write_bytes(primary_original)
    backup.write_bytes(backup_original)

    with pytest.raises(ledger.SignalLedgerCorruptionError, match="line 2"):
        ledger.sync_backup(str(path))

    assert path.read_bytes() == primary_original
    assert backup.read_bytes() == backup_original


def test_backup_sync_fails_closed_when_primary_is_corrupt_and_backup_is_absent(
    tmp_path,
    monkeypatch,
):
    path, backup = _backup_paths(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    original = (_ledger_line("evt-before") + "\n" + "not-json\n").encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(ledger.SignalLedgerCorruptionError, match="line 2"):
        ledger.sync_backup(str(path))

    assert path.read_bytes() == original
    assert not backup.exists()


def test_backup_sync_does_not_overwrite_a_corrupt_existing_backup(tmp_path, monkeypatch):
    path, backup = _backup_paths(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    backup.parent.mkdir(parents=True)
    path.write_text(_ledger_line("evt-primary") + "\n", encoding="utf-8")
    backup_original = b"truncated-backup"
    backup.write_bytes(backup_original)

    with pytest.raises(ledger.SignalLedgerCorruptionError, match="line 1"):
        ledger.sync_backup(str(path))

    assert backup.read_bytes() == backup_original


def test_empty_signal_ledger_is_valid(tmp_path):
    path = tmp_path / "signal_ledger.jsonl"
    path.write_bytes(b"")

    assert ledger.read_events(str(path)) == []


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


def test_opened_signal_preserves_research_strategy_attributions():
    links = ledger.make_links("rec-attribution")
    opened = ledger.signal_opened_event(
        {
            "code": "002156",
            "date": "2026-06-12",
            "entry_price": 11.0,
            "strategy_id": "trend_pullback",
            "strategy_attributions": [
                {
                    "strategy_id": "chanlun_third_buy",
                    "role": "research_evidence",
                    "direction": "bullish",
                    "signal_type": "third_buy",
                }
            ],
        },
        links,
    )

    assert opened["payload"]["strategy_attributions"][0]["strategy_id"] == "chanlun_third_buy"


def test_opened_signal_preserves_evidence_sources():
    links = ledger.make_links("rec-evidence")
    opened = ledger.signal_opened_event(
        {
            "code": "002156",
            "date": "2026-06-12",
            "entry_price": 11.0,
            "strategy_id": "trend_pullback",
            "evidence_sources": [
                {
                    "source": "open-confirmation",
                    "artifact": {"snapshot_id": "snap-1"},
                    "weight_hint": "primary",
                },
                {
                    "source": "auction-finalize",
                    "artifact": {"path": "auction_shortlist_2026-06-12.json"},
                    "weight_hint": "supporting",
                },
            ],
        },
        links,
    )

    record = ledger.project_signals([opened])[0]

    assert record["evidence_sources"] == [
        {
            "source": "open-confirmation",
            "artifact": {"snapshot_id": "snap-1"},
            "weight_hint": "primary",
        },
        {
            "source": "auction-finalize",
            "artifact": {"path": "auction_shortlist_2026-06-12.json"},
            "weight_hint": "supporting",
        },
    ]


def test_opened_signal_preserves_social_attention_attribution():
    event = ledger.signal_opened_event(
        {
            "code": "002156",
            "name": "通富微电",
            "date": "2026-06-15",
            "entry_price": 23.5,
            "strategy_id": "daban:first_board_reseal",
            "action": "buy",
            "social_attention": {
                "candidate_bonus": 3.0,
                "auction_delta": 1.5,
                "record": {
                    "attention_score": 88,
                    "cross_source_count": 2,
                },
            },
        },
        ledger.make_links("rec-social"),
    )

    assert event["payload"]["social_attention"]["candidate_bonus"] == 3.0
    assert event["payload"]["social_attention"]["record"]["cross_source_count"] == 2


def test_signal_ledger_accepts_monitor_events_as_canonical_state_changes(tmp_path):
    path = str(tmp_path / "signal_ledger.jsonl")
    event = ledger.append_event(
        "monitor.activated",
        ledger.make_links("rec-x", monitor_id="stock:600011"),
        {
            "id": "stock:600011",
            "kind": "stock",
            "key": "600011",
            "status": "active",
            "manual_cancelled": False,
        },
        idempotency_key="monitor.activated:stock:600011:batch-1",
        ledger_file=path,
    )

    assert event["sequence"] == 1
    assert ledger.read_events(path) == [event]


def _tail_provenance():
    return {
        "decision_mode": "live",
        "snapshot_id": "tail-close:2026-07-28",
        "snapshot_hash": "a" * 64,
        "config_hash": "b" * 64,
        "code_version": "b855b86",
    }


def _tail_record(**overrides):
    record = {
        "strategy_id": "tail_close_signal_v1",
        "signal_date": "2026-07-28",
        "code": "600001",
        "provenance": _tail_provenance(),
    }
    record.update(overrides)
    return record


def _manual_tail_record(**overrides):
    values = {
        "status": "matched",
        "pilot_gate_hash": "1" * 64,
        "simulation_fill_hash": "2" * 64,
        "evidence_hash": "3" * 64,
        "human_approval_id": "approval-tail-1",
        "human_approved_at": "2026-07-29T10:00:00+08:00",
        "actual_filled_quantity": 100,
        "actual_fill_price": 10.03,
        "external_broker_evidence_confirmed": True,
    }
    values.update(overrides)
    return _tail_record(**values)


def test_tail_close_research_lifecycle_helpers_are_stable_and_simulation_only():
    links = ledger.make_links(
        correlation_id="corr-tail-1",
        signal_id="tail-signal-1",
        trade_id="tail-simulated-trade-1",
    )
    signal = ledger.research_signal_event(_tail_record(), links)
    order = ledger.simulated_order_event(
        _tail_record(side="buy", quantity=100, limit_price=10.0),
        links,
    )
    fill = ledger.simulated_fill_event(
        _tail_record(
            side="buy",
            quantity=100,
            fill_price=10.02,
            fill_hash="d" * 64,
        ),
        links,
    )
    automatic = ledger.simulation_reconciliation_event(
        _tail_record(
            status="FULL_FILL",
            decision_hash="c" * 64,
            fill_hash="d" * 64,
        ),
        links,
    )
    reconciliation = ledger.manual_reconciliation_event(
        _manual_tail_record(reconciled_by="operator"),
        links,
    )

    assert [
        signal["event_type"],
        order["event_type"],
        fill["event_type"],
        automatic["event_type"],
        reconciliation["event_type"],
    ] == [
        "tail_close.signal_created",
        "tail_close.order_simulated",
        "tail_close.fill_simulated",
        "tail_close.simulation_reconciled",
        "tail_close.manual_reconciled",
    ]
    assert signal["idempotency_key"] == ledger.research_signal_event(
        _tail_record(), links
    )["idempotency_key"]
    for event in (signal, order, fill, automatic, reconciliation):
        assert event["payload"]["research_only"] is True
        assert event["payload"]["live_order_sent"] is False
        assert event["payload"]["provenance"] == _tail_provenance()
    assert signal["payload"]["execution_action"] == "none"
    assert order["payload"]["execution_mode"] == "simulated"
    assert fill["payload"]["execution_mode"] == "simulated"
    assert automatic["payload"]["reconciliation_mode"] == "automatic_simulation"
    assert automatic["payload"]["broker_confirmed"] is False
    assert (
        reconciliation["payload"]["reconciliation_mode"]
        == "manual_external_evidence"
    )
    assert reconciliation["payload"]["execution_mode"] == (
        "manual_external_reconciliation"
    )
    assert reconciliation["payload"]["simulation"] is False
    assert reconciliation["payload"]["broker_confirmed"] is True


def test_tail_close_helpers_require_complete_content_addressed_provenance():
    links = ledger.make_links(signal_id="tail-signal-2")
    for missing in _tail_provenance():
        provenance = _tail_provenance()
        provenance.pop(missing)
        with pytest.raises(ValueError, match=f"provenance.{missing}"):
            ledger.research_signal_event(
                _tail_record(provenance=provenance),
                links,
            )

    with pytest.raises(ValueError, match="provenance.snapshot_hash"):
        ledger.research_signal_event(
            _tail_record(
                provenance={**_tail_provenance(), "snapshot_hash": "not-a-hash"}
            ),
            links,
        )


def test_tail_close_helpers_reject_conflicting_signal_linkage():
    with pytest.raises(ValueError, match="signal_id conflicts"):
        ledger.research_signal_event(
            _tail_record(signal_id="record-signal"),
            ledger.make_links(signal_id="linked-signal"),
        )


@pytest.mark.parametrize(
    "helper,record",
    [
        (
            ledger.simulated_order_event,
            _tail_record(execution_mode="live", live_order_sent=True),
        ),
        (
            ledger.simulated_fill_event,
            _tail_record(broker_order_id="real-order-1"),
        ),
        (
            ledger.manual_reconciliation_event,
            _manual_tail_record(account_mode="real"),
        ),
        (
            ledger.simulated_fill_event,
            _tail_record(broker_called=True),
        ),
    ],
)
def test_tail_close_simulation_helpers_reject_real_execution_markers(helper, record):
    with pytest.raises(ValueError, match="real execution"):
        helper(record, ledger.make_links(signal_id="tail-signal-3"))


def test_manual_reconciliation_requires_confirmed_external_broker_evidence():
    with pytest.raises(ValueError, match="broker evidence unconfirmed"):
        ledger.manual_reconciliation_event(
            _manual_tail_record(external_broker_evidence_confirmed=False),
            ledger.make_links(signal_id="tail-signal-unconfirmed"),
        )


def test_tail_close_events_do_not_pollute_existing_signal_projection(tmp_path):
    path = str(tmp_path / "signal_ledger.jsonl")
    links = ledger.make_links(signal_id="tail-signal-4")
    events = [
        ledger.research_signal_event(_tail_record(), links),
        ledger.simulated_order_event(_tail_record(side="buy", quantity=100), links),
        ledger.simulated_fill_event(
            _tail_record(
                side="buy",
                quantity=100,
                fill_price=10.02,
                fill_hash="d" * 64,
            ),
            links,
        ),
        ledger.simulation_reconciliation_event(
            _tail_record(
                status="FULL_FILL",
                decision_hash="c" * 64,
                fill_hash="d" * 64,
            ),
            links,
        ),
        ledger.manual_reconciliation_event(
            _manual_tail_record(simulation_fill_hash="d" * 64),
            links,
        ),
    ]

    ledger.append_events(events + events, ledger_file=path)

    assert len(ledger.read_events(path)) == 5
    assert ledger.project_signals(ledger_file=path) == []
    lifecycle = ledger.project_tail_close_lifecycle(ledger_file=path)
    assert len(lifecycle) == 1
    assert lifecycle[0]["complete"] is True
    assert lifecycle[0]["violations"] == []


def test_tail_close_idempotency_rejects_conflicting_fact(tmp_path):
    path = str(tmp_path / "signal_ledger.jsonl")
    links = ledger.make_links(signal_id="tail-signal-conflict")
    first = ledger.simulated_fill_event(
        _tail_record(side="buy", quantity=100, fill_price=10.02),
        links,
    )
    conflicting = ledger.simulated_fill_event(
        _tail_record(side="buy", quantity=900, fill_price=10.02),
        links,
    )

    ledger.append_events([first], ledger_file=path)

    with pytest.raises(ValueError, match="idempotency conflict"):
        ledger.append_events([conflicting], ledger_file=path)
    assert ledger.read_events(path)[0]["payload"]["quantity"] == 100


def test_tail_close_projection_reports_missing_and_mismatched_reconciliation():
    links = ledger.make_links(
        correlation_id="corr-tail-audit",
        signal_id="tail-signal-audit",
    )
    events = [
        ledger.research_signal_event(_tail_record(), links),
        ledger.simulated_order_event(_tail_record(quantity=100), links),
        ledger.simulated_fill_event(
            _tail_record(quantity=100, fill_hash="d" * 64),
            links,
        ),
        ledger.simulation_reconciliation_event(
            _tail_record(
                decision_hash="c" * 64,
                fill_hash="e" * 64,
            ),
            links,
        ),
    ]
    normalized = []
    for sequence, event in enumerate(events, start=1):
        normalized.append(
            {
                **event,
                "event_id": f"event-{sequence}",
                "sequence": sequence,
            }
        )

    lifecycle = ledger.project_tail_close_lifecycle(normalized)[0]
    assert lifecycle["complete"] is False
    assert lifecycle["violations"] == ["fill_hash_mismatch"]

    missing = ledger.project_tail_close_lifecycle(normalized[:-1])[0]
    assert missing["complete"] is False
    assert missing["violations"] == ["reconciliation_missing"]

    manual = ledger.manual_reconciliation_event(_manual_tail_record(), links)
    with_manual = [
        *normalized,
        {
            **manual,
            "event_id": "event-5",
            "sequence": 5,
        },
    ]
    manual_mismatch = ledger.project_tail_close_lifecycle(with_manual)[0]
    assert manual_mismatch["complete"] is False
    assert manual_mismatch["violations"] == ["fill_hash_mismatch", "manual_fill_hash_mismatch"]


# ---------------------------------------------------------------------------
# issue #260 §4.C.4: conditional_buy is a legal recommendation action but must
# never create a trade or settlement fact — it stays outside both sets.
# ---------------------------------------------------------------------------


def test_conditional_buy_is_not_a_trade_or_settleable_action():
    assert "conditional_buy" not in ledger.TRADE_ACTIONS
    assert "conditional_buy" not in ledger.SETTLEABLE_ACTIONS


def test_trade_and_settleable_actions_stay_confined_to_buy_and_add():
    """锁住既有口径，防止未来有人往里加 conditional_buy 或其它非成交动作。"""
    assert ledger.TRADE_ACTIONS == {"buy", "add"}
    assert ledger.SETTLEABLE_ACTIONS == {"buy", "add"}
