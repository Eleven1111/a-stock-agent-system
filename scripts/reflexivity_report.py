#!/usr/bin/env python3
"""Research-only cost-adjusted ablation for reflexivity defensive guards."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")


def _load_common_module(name: str):
    """Load a common module without changing the process-wide import path."""
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(COMMON, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load common module: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load_common_module("paths")
_load_common_module("state_store")
la = _load_common_module("lifecycle_analytics")


def build_report(
    *,
    days: list[str] | None = None,
    outcome_key: str = "t3_close_ret",
    round_trip_cost_bps: float = 20.0,
    expected_config_sha256: str | None = None,
) -> dict:
    settled_days = days if days is not None else la.available_days()
    records = la.load_settled_records(settled_days)
    analysis = la.reflexivity_ablation(
        records,
        outcome_key=outcome_key,
        round_trip_cost_bps=round_trip_cost_bps,
        expected_config_sha256=expected_config_sha256,
    )
    return {
        "schema": "reflexivity_ablation_report_v1",
        **analysis,
        "settled_days": settled_days,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", action="append", dest="days")
    parser.add_argument("--outcome", default="t3_close_ret", choices=la.OUTCOME_KEYS)
    parser.add_argument("--round-trip-cost-bps", type=float, default=20.0)
    parser.add_argument("--config-sha256", dest="expected_config_sha256")
    args = parser.parse_args()
    print(json.dumps(build_report(
        days=args.days,
        outcome_key=args.outcome,
        round_trip_cost_bps=args.round_trip_cost_bps,
        expected_config_sha256=args.expected_config_sha256,
    ), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
