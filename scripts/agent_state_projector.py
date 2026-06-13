#!/usr/bin/env python3
"""Project the canonical ledger into one Hermes/OpenClaw decision surface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

import monitor_registry  # noqa: E402
from agent_state import agent_state_path  # noqa: E402
from paths import data_file  # noqa: E402
import signal_ledger  # noqa: E402
import serenity_refresh_queue  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402
import strategy_registry  # noqa: E402


OUTPUT_FILE = agent_state_path()


def build_agent_state(
    *,
    ledger_file: Optional[str] = None,
    portfolio: Optional[dict[str, Any]] = None,
    monitors: Optional[list[dict[str, Any]]] = None,
    strategies: Optional[dict[str, Any]] = None,
    serenity_requests: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    events = signal_ledger.read_events(ledger_file)
    recommendations: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") != "recommendation.created":
            continue
        payload = dict(event.get("payload") or {})
        rec_id = str((event.get("links") or {}).get("recommendation_id") or payload.get("id") or "")
        if rec_id:
            recommendations[rec_id] = payload
    signals = signal_ledger.project_signals(events)
    pending = [
        record for record in signals
        if record.get("settlement_status", "pending") != "final"
    ]
    return {
        "schema": "a_stock_agent_state_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime_contract": {
            "state_root_env": "A_STOCK_STATE_HOME",
            "supported_runtimes": ["hermes", "openclaw"],
            "source_of_truth": "signal_ledger.jsonl",
            "required_loader": "python scripts/agent_runtime_context.py",
            "cross_host_coordination": "shared_filesystem_required",
        },
        "portfolio": portfolio if portfolio is not None else read_json(
            data_file("stock-triage", "portfolio.json"),
            {"cash": 0, "positions": []},
        ),
        "recommendations": list(recommendations.values()),
        "signals": signals,
        "pending_settlements": pending,
        "monitors": monitors if monitors is not None else monitor_registry.active_entries(),
        "strategies": strategies if strategies is not None else strategy_registry.all_strategies(),
        "serenity_refresh_requests": (
            serenity_requests
            if serenity_requests is not None
            else serenity_refresh_queue.pending_requests(
                data_file("stock-triage", "serenity_refresh_queue.json")
            )
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    state = build_agent_state()
    atomic_write_json(args.output or agent_state_path(), state)
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
