import pytest

import agent_state
from state_store import atomic_write_json


def test_agent_state_is_required_for_runtime_reasoning(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    with pytest.raises(agent_state.AgentStateUnavailable):
        agent_state.load_agent_state(required=True)


def test_agent_state_loader_validates_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    atomic_write_json(
        agent_state.agent_state_path(),
        {
            "schema": "a_stock_agent_state_v1",
            "generated_at": "2026-06-12T09:35:00+08:00",
            "portfolio": {"cash": 100000, "positions": []},
            "recommendations": [],
            "signals": [],
            "monitors": [],
            "strategies": {},
        },
    )

    state = agent_state.load_agent_state(required=True)

    assert state["portfolio"]["cash"] == 100000
