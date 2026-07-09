#!/usr/bin/env python3
"""Scheduled behavioral finance context digest."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

from behavioral_finance import build_behavioral_finance_context  # noqa: E402
from paths import cache_dir, data_file  # noqa: E402
from runtime_context import load_latest_artifact, make_batch_id  # noqa: E402
from signal_ledger import project_signals  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402


def _parsed_stdout(artifact: dict[str, Any] | None) -> dict[str, Any] | None:
    if not artifact:
        return None
    try:
        parsed = json.loads(artifact.get("stdout") or "")
    except (TypeError, json.JSONDecodeError):
        parsed = None
    return parsed if isinstance(parsed, dict) else None


def _latest_payload(job_id: str) -> dict[str, Any] | None:
    return _parsed_stdout(load_latest_artifact(job_id))


def run_digest(stage: str) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    trading_date = os.environ.get("A_STOCK_TRADING_DATE") or now[:10]
    batch_id = os.environ.get("A_STOCK_BATCH_ID") or make_batch_id(trading_date)
    social_job = "social-attention-preopen" if stage == "preopen" else "social-attention-close"
    social = _latest_payload(social_job) or read_json(
        os.path.join(cache_dir("stock-triage"), "social_attention.json"),
        {},
    )
    hot_money = _latest_payload("hot-money-context") or read_json(
        os.path.join(cache_dir("stock-triage"), "signal_context.json"),
        {},
    )
    market_snapshot = _latest_payload("candidate-discovery") or read_json(
        data_file("stock-triage", "candidate_pool_latest.json"),
        {},
    )
    try:
        signal_state = project_signals()
    except Exception:  # noqa: BLE001
        signal_state = read_json(data_file("stock-triage", "signal_ledger.json"), {"signals": []})
    result = build_behavioral_finance_context(
        market_snapshot,
        social,
        hot_money,
        signal_state,
        asof=now,
        trading_date=trading_date,
        batch_id=batch_id,
    )
    result["stage"] = stage
    path = os.path.join(cache_dir("stock-triage"), "behavioral_finance_context.json")
    atomic_write_json(path, result)
    result["output_path"] = path
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build behavioral finance digest")
    parser.add_argument("--stage", choices=["preopen", "close"], required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--json-lines-safe", action="store_true")
    args = parser.parse_args()
    result = run_digest(args.stage)
    if args.json or args.json_lines_safe:
        print(json.dumps(result, ensure_ascii=False, indent=None if args.json_lines_safe else 2))
    else:
        print(result["summary"])


if __name__ == "__main__":
    main()
