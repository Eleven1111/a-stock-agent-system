import signal_ledger
from scripts.agent_state_projector import build_agent_state


def test_projector_exposes_one_runtime_neutral_decision_surface(tmp_path):
    ledger_file = str(tmp_path / "signal_ledger.jsonl")
    links = signal_ledger.make_links("rec-1", include_trade=True)
    signal_ledger.append_events(
        [
            {
                "event_type": "recommendation.created",
                "links": links,
                "payload": {
                    "id": "rec-1",
                    "code": "002156",
                    "name": "通富微电",
                    "action": "buy",
                    "quality_report": {"status": "passed"},
                },
                "idempotency_key": "rec-1",
            },
            signal_ledger.signal_opened_event(
                {
                    "code": "002156",
                    "name": "通富微电",
                    "date": "2026-06-12",
                    "entry_price": 11.0,
                    "strategy_id": "trend_pullback",
                    "action": "buy",
                },
                links,
            ),
        ],
        ledger_file=ledger_file,
    )

    state = build_agent_state(
        ledger_file=ledger_file,
        portfolio={"cash": 50000, "positions": []},
        monitors=[],
        strategies={},
        serenity_requests=[
            {
                "id": "serenity-002156-2026-06-12",
                "code": "002156",
                "status": "pending",
            }
        ],
    )

    assert state["schema"] == "a_stock_agent_state_v1"
    assert state["recommendations"][0]["code"] == "002156"
    assert state["signals"][0]["settlement_status"] == "pending"
    assert state["runtime_contract"]["state_root_env"] == "A_STOCK_STATE_HOME"
    assert state["runtime_contract"]["cross_host_coordination"] == "shared_filesystem_required"
    assert state["serenity_refresh_requests"][0]["code"] == "002156"
