#!/usr/bin/env python3
"""Probe external market datasets and report endpoint-specific health."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from typing import Any, Callable

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

from a_share_rules import previous_trading_day  # noqa: E402
from eastmoney_intelligence import eastmoney_json  # noqa: E402
from market_adapters import (  # noqa: E402
    fetch_a_share_spot,
    fetch_hot_money_limitup_pool,
    fetch_industry_catalog_ths,
    fetch_tencent_quote,
)


def _limitup_probe():
    asof = previous_trading_day(date.today()).strftime("%Y%m%d")
    return fetch_hot_money_limitup_pool(asof)


def _eastmoney_flow_probe():
    return eastmoney_json(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?"
        "fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56&lmt=1&secid=1.600519",
        required_path=("data", "klines"),
        required_type=list,
    )


PROBES: dict[str, dict[str, Any]] = {
    "tencent_quote": {
        "provider": "tencent",
        "required": True,
        "call": lambda: fetch_tencent_quote(["sh000001"]),
    },
    "eastmoney_fund_flow": {
        "provider": "eastmoney_push2his",
        "required": False,
        "call": _eastmoney_flow_probe,
    },
    "akshare_limitup": {
        "provider": "akshare_push2ex",
        "required": False,
        "call": _limitup_probe,
    },
    "ths_industry_catalog": {
        "provider": "akshare_ths",
        "required": False,
        "call": fetch_industry_catalog_ths,
    },
    "akshare_spot_em": {
        "provider": "akshare_push2",
        "required": False,
        "call": fetch_a_share_spot,
    },
}


def _row_count(value: Any) -> int | None:
    if value is None:
        return 0
    if isinstance(value, dict):
        return len(value)
    try:
        return len(value)
    except TypeError:
        return None


def _run_probe(
    name: str,
    provider: str,
    required: bool,
    call: Callable[[], Any],
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        value = call()
        rows = _row_count(value)
        if rows == 0:
            raise ValueError("provider returned an empty dataset")
        return {
            "dataset": name,
            "provider": provider,
            "required": required,
            "status": "ok",
            "row_count": rows,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "dataset": name,
            "provider": provider,
            "required": required,
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "duration_seconds": round(time.monotonic() - started, 3),
        }


def run_probes() -> dict[str, Any]:
    datasets = {
        name: _run_probe(
            name,
            str(spec["provider"]),
            bool(spec["required"]),
            spec["call"],
        )
        for name, spec in PROBES.items()
    }
    required_failed = any(
        item["required"] and item["status"] != "ok"
        for item in datasets.values()
    )
    optional_failed = any(item["status"] != "ok" for item in datasets.values())
    return {
        "schema": "a_stock_provider_health_v1",
        "status": "error" if required_failed else "degraded" if optional_failed else "ok",
        "datasets": datasets,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.parse_args()
    report = run_probes()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
