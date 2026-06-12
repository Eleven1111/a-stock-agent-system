#!/usr/bin/env python3
"""Refresh and emit the canonical state required by Hermes/OpenClaw reasoning."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)
sys.path.insert(0, ROOT)

from agent_state import agent_state_path, load_agent_state  # noqa: E402
from scripts.agent_state_projector import build_agent_state  # noqa: E402
from state_store import atomic_write_json  # noqa: E402


def build_runtime_context(*, refresh: bool = True) -> dict[str, Any]:
    if refresh:
        atomic_write_json(agent_state_path(), build_agent_state())
    state = load_agent_state(required=True)
    return {
        "schema": "a_stock_agent_runtime_context_v1",
        "state_path": agent_state_path(),
        "generated_at": state.get("generated_at"),
        "portfolio": state.get("portfolio"),
        "recommendations": state.get("recommendations"),
        "signals": state.get("signals"),
        "pending_settlements": state.get("pending_settlements"),
        "monitors": state.get("monitors"),
        "strategies": state.get("strategies"),
        "runtime_contract": state.get("runtime_contract"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-refresh", action="store_true")
    args = parser.parse_args()
    print(json.dumps(
        build_runtime_context(refresh=not args.no_refresh),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
