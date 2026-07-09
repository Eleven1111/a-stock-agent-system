#!/usr/bin/env python3
"""Company event opportunity scan entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

from a_share_rules import latest_trading_day  # noqa: E402
from company_event_opportunities import (  # noqa: E402
    load_default_source_payloads,
    scan_company_event_opportunities,
    write_company_event_outputs,
)
from runtime_context import make_batch_id  # noqa: E402
from runtime_targets import load_stock_targets  # noqa: E402


def run_scan(scope: str = "runtime") -> dict:
    trading_date = os.environ.get("A_STOCK_TRADING_DATE") or os.environ.get("HERMES_TRADING_DATE")
    if not trading_date:
        trading_date = latest_trading_day(datetime.now()).isoformat()
    batch_id = (
        os.environ.get("A_STOCK_BATCH_ID")
        or os.environ.get("HERMES_BATCH_ID")
        or make_batch_id(trading_date)
    )
    targets = load_stock_targets(candidate_limit=80) if scope == "runtime" else []
    result = scan_company_event_opportunities(
        targets=targets,
        source_payloads=load_default_source_payloads(),
        trading_date=trading_date,
        batch_id=batch_id,
    )
    result["output_paths"] = write_company_event_outputs(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan company event opportunities")
    parser.add_argument("--scope", choices=["runtime"], default="runtime")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--json-lines-safe", action="store_true")
    args = parser.parse_args()
    result = run_scan(scope=args.scope)
    if args.json or args.json_lines_safe:
        print(json.dumps(result, ensure_ascii=False, indent=None if args.json_lines_safe else 2))
    else:
        print(result.get("summary", {}))


if __name__ == "__main__":
    main()
