"""Validated runtime-neutral state consumed by Hermes and OpenClaw agents."""

from __future__ import annotations

import os
from typing import Any

from paths import hermes_home
from state_store import read_json


SCHEMA = "a_stock_agent_state_v1"


class AgentStateUnavailable(RuntimeError):
    pass


def agent_state_path() -> str:
    return os.environ.get("A_STOCK_AGENT_STATE_PATH") or os.path.join(
        hermes_home(),
        "agent_state",
        "agent_state_latest.json",
    )


def load_agent_state(*, required: bool = False) -> dict[str, Any] | None:
    path = agent_state_path()
    state = read_json(path, None)
    if not isinstance(state, dict) or state.get("schema") != SCHEMA:
        if required:
            raise AgentStateUnavailable(
                f"统一 Agent State 不可用: {path}; 先运行 ledger-projector"
            )
        return None
    return state
