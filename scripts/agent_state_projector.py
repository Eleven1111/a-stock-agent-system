#!/usr/bin/env python3
"""Project the canonical ledger into one Hermes/OpenClaw decision surface."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import monitor_registry  # noqa: E402
import behavior_risk  # noqa: E402
from agent_state import agent_state_path  # noqa: E402
from paths import data_file  # noqa: E402
import research_bus  # noqa: E402
import signal_ledger  # noqa: E402
import serenity_refresh_queue  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402
import strategy_registry  # noqa: E402


OUTPUT_FILE = agent_state_path()

# Token-bounded projection. The canonical on-disk state stays full; only the
# surface a model loads is slimmed. Heavy audit/provenance blocks are dropped
# here and remain reachable through the full state or signal_ledger.jsonl.
LITE_REC_FIELDS = (
    "id",
    "code",
    "name",
    "date",
    "action",
    "grade",
    "confidence",
    "entry_price",
    "price_range",
    "target_price",
    "stop_price",
    "horizon",
    "rationale",
    "strategy_id",
    "outcome",
    "settleable_signal",
)
LITE_MONITOR_FIELDS = ("key", "label", "kind", "status", "source_group")


def project_recommendation_lite(rec: dict[str, Any]) -> dict[str, Any]:
    out = {key: rec[key] for key in LITE_REC_FIELDS if key in rec}
    risks = rec.get("risks")
    if isinstance(risks, list) and risks:
        out["top_risks"] = risks[:3]
    sizing = rec.get("position_sizing")
    if isinstance(sizing, dict):
        for src, dst in (
            ("recommended_position_pct", "position_pct"),
            ("gating_status", "position_status"),
        ):
            if sizing.get(src) is not None:
                out[dst] = sizing[src]
    constraints = rec.get("execution_constraints")
    if isinstance(constraints, dict):
        for src, dst in (
            ("earliest_sell_date", "earliest_sell_date"),
            ("same_day_sell_allowed", "same_day_sell_allowed"),
        ):
            if constraints.get(src) is not None:
                out[dst] = constraints[src]
    return out


def project_monitor_lite(monitor: dict[str, Any]) -> dict[str, Any]:
    return {key: monitor[key] for key in LITE_MONITOR_FIELDS if key in monitor}


def _bus_serenity_requests() -> list[dict[str, Any]]:
    """Pending serenity_refresh research-bus tasks, projected into the same
    shape the legacy standalone-queue pending_requests() returned (id, code,
    name, status, priority) so downstream readers of
    ``serenity_refresh_requests`` stay compatible after the §6a bus merge."""
    requests = []
    for task in research_bus.load_tasks():
        if task.get("kind") != "serenity_refresh":
            continue
        if task.get("status") not in research_bus.ACTIVE_STATUSES:
            continue
        subject = task.get("subject") or {}
        requests.append({
            "id": task.get("id"),
            "code": subject.get("code"),
            "name": subject.get("name"),
            "status": task.get("status"),
            "priority": task.get("priority"),
            "reason": task.get("reason"),
        })
    return requests


def default_serenity_refresh_requests() -> list[dict[str, Any]]:
    """Bus-sourced pending serenity_refresh tasks, plus any pending/claimed
    items still sitting in the deprecated standalone queue file (a deployed
    host may have a historical backlog to drain — see
    serenity_refresh_queue.py's module docstring)."""
    return _bus_serenity_requests() + serenity_refresh_queue.pending_requests(
        data_file("stock-triage", "serenity_refresh_queue.json")
    )


def project_state_lite(state: dict[str, Any]) -> dict[str, Any]:
    """Return a token-bounded view of an agent state for model consumption."""
    projected = dict(state)
    recommendations = state.get("recommendations")
    if isinstance(recommendations, list):
        projected["recommendations"] = [
            project_recommendation_lite(rec)
            for rec in recommendations
            if isinstance(rec, dict)
        ]
    monitors = state.get("monitors")
    if isinstance(monitors, list):
        projected["monitors"] = [
            project_monitor_lite(monitor)
            for monitor in monitors
            if isinstance(monitor, dict)
        ]
    return projected


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
        "behavior_risk": behavior_risk.assess_behavior_risk(
            signals,
            asof=datetime.now().date().isoformat(),
        ),
        "pending_settlements": pending,
        "monitors": monitors if monitors is not None else monitor_registry.active_entries(),
        "strategies": strategies if strategies is not None else strategy_registry.all_strategies(),
        "serenity_refresh_requests": (
            serenity_requests
            if serenity_requests is not None
            else default_serenity_refresh_requests()
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Print the full canonical state instead of the token-bounded view",
    )
    args = parser.parse_args()
    state = build_agent_state()
    atomic_write_json(args.output or agent_state_path(), state)
    emitted = state if args.full else project_state_lite(state)
    print(json.dumps(emitted, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
