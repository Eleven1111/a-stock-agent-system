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
from scripts.agent_state_projector import (  # noqa: E402
    build_agent_state,
    project_recommendation_lite,
    project_monitor_lite,
)
from state_store import atomic_write_json  # noqa: E402


def build_runtime_context(*, refresh: bool = True, lite: bool = True) -> dict[str, Any]:
    if refresh:
        atomic_write_json(agent_state_path(), build_agent_state())
    state = load_agent_state(required=True)
    recommendations = state.get("recommendations") or []
    monitors = state.get("monitors") or []
    if lite:
        recommendations = [
            project_recommendation_lite(rec)
            for rec in recommendations
            if isinstance(rec, dict)
        ]
        monitors = [
            project_monitor_lite(monitor)
            for monitor in monitors
            if isinstance(monitor, dict)
        ]
    return {
        "schema": "a_stock_agent_runtime_context_v1",
        "view": "lite" if lite else "full",
        "state_path": agent_state_path(),
        "generated_at": state.get("generated_at"),
        "portfolio": state.get("portfolio"),
        "recommendations": recommendations,
        "signals": state.get("signals"),
        "behavior_risk": state.get("behavior_risk"),
        "pending_settlements": state.get("pending_settlements"),
        "monitors": monitors,
        "strategies": state.get("strategies"),
        "serenity_refresh_requests": state.get("serenity_refresh_requests"),
        "runtime_contract": state.get("runtime_contract"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Emit every recommendation/monitor field instead of the lite view",
    )
    args = parser.parse_args()
    context = build_runtime_context(refresh=not args.no_refresh, lite=not args.full)
    if args.full:
        print(json.dumps(context, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(context, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
