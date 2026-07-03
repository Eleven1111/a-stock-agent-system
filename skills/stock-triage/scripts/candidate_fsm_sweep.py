#!/usr/bin/env python3
"""Daily timeout sweep for the candidate FSM.

Drops `watching` candidates that have gone stale (no new evidence for N
trading days) and downgrades `confirmed` candidates that have stalled without
a fill for M trading days back to `candidate`. Both thresholds come from
config/candidate_selection.json's `candidate_fsm.timeouts` section. This is
plain deterministic code: no model call, no network I/O beyond reading local
FSM transition-log state.

Usage:
  python candidate_fsm_sweep.py --asof 2026-07-03 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from typing import Any

SCRIPT_DIR = os.path.dirname(__file__)
COMMON = os.path.join(SCRIPT_DIR, "..", "..", "common")
sys.path.insert(0, COMMON)

import candidate_fsm  # noqa: E402


def run_sweep(asof: str) -> dict[str, Any]:
    config = candidate_fsm.load_fsm_config()
    watching_codes = candidate_fsm.codes_in_state("watching")
    confirmed_codes = candidate_fsm.codes_in_state("confirmed")

    stale_watch_events = candidate_fsm.sweep_stale_watch(asof, watching_codes, config=config)
    confirm_stall_events = candidate_fsm.sweep_confirm_stall(asof, confirmed_codes, config=config)

    return {
        "schema": "candidate_fsm_sweep_v1",
        "asof": asof,
        "status": "ready",
        "watching_scanned": len(watching_codes),
        "confirmed_scanned": len(confirmed_codes),
        "dropped_stale_watch": [event["code"] for event in stale_watch_events],
        "downgraded_confirm_stall": [event["code"] for event in confirm_stall_events],
        "dropped_count": len(stale_watch_events),
        "downgraded_count": len(confirm_stall_events),
    }


def format_report(result: dict[str, Any]) -> str:
    lines = [
        f"## 候选状态机超时清扫 | {result['asof']}",
        f"观察中扫描 {result['watching_scanned']} | 淘汰(stale_watch) {result['dropped_count']}",
        f"已确认扫描 {result['confirmed_scanned']} | 回退(confirm_stall) {result['downgraded_count']}",
    ]
    if result["dropped_stale_watch"]:
        lines.append("淘汰: " + ", ".join(result["dropped_stale_watch"]))
    if result["downgraded_confirm_stall"]:
        lines.append("回退: " + ", ".join(result["downgraded_confirm_stall"]))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="候选状态机超时清扫")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_sweep(args.asof)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
