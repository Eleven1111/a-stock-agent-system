#!/usr/bin/env python3
"""Live smoke test for the resilient A-share data-source fallback chain.

This script intentionally exercises the same paths used by cron jobs:
spot/quote, daily and minute bars, stock/sector fund flow, dragon-tiger
datacenter reports, board quotes, northbound flow, and bounded concurrent
kline calls.  It reports failures as data-source failures; it does not treat
network errors as neutral evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Callable

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from market_adapters import (  # noqa: E402
    fetch_a_share_daily_kline,
    fetch_a_share_quote,
    fetch_a_share_spot,
    fetch_board_quotes,
    fetch_dragon_tiger_rows,
    fetch_northbound_flow,
    fetch_sector_fund_flow,
    fetch_stock_fund_flow,
    fetch_tencent_minute,
)
import provider_health  # noqa: E402


SAMPLE_CODES = ["600519", "000001", "300750", "688981", "002371"]


def _count(value: Any) -> int:
    if value is None:
        return 0
    if hasattr(value, "empty") and hasattr(value, "__len__"):
        return 0 if value.empty else len(value)
    if isinstance(value, dict):
        return 1 if value else 0
    try:
        return len(value)
    except TypeError:
        return 1


def _probe(name: str, call: Callable[[], Any], *, required: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    try:
        value = call()
        rows = _count(value)
        if rows == 0:
            raise RuntimeError("empty result")
        return {
            "name": name,
            "required": required,
            "status": "ok",
            "row_count": rows,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": name,
            "required": required,
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "duration_seconds": round(time.monotonic() - started, 3),
        }


def _concurrent_kline_probe(workers: int) -> dict[str, Any]:
    started = time.monotonic()
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                fetch_a_share_daily_kline,
                code,
                market="sh" if code.startswith("6") else "sz",
                days=10,
            ): code
            for code in SAMPLE_CODES
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                rows = future.result()
                results[code] = {"status": "ok" if rows else "empty", "rows": len(rows)}
            except Exception as exc:  # noqa: BLE001
                results[code] = {"status": "error", "error": str(exc)[:300]}
    ok = sum(1 for item in results.values() if item["status"] == "ok")
    return {
        "name": "concurrent_daily_kline",
        "required": True,
        "status": "ok" if ok >= max(3, len(SAMPLE_CODES) // 2) else "error",
        "ok_count": ok,
        "results": results,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def run(workers: int) -> dict[str, Any]:
    probes = [
        _probe("a_share_spot", fetch_a_share_spot, required=True),
        _probe("single_quote", lambda: fetch_a_share_quote("600519"), required=True),
        _probe("daily_kline", lambda: fetch_a_share_daily_kline("600519", market="sh", days=10), required=True),
        _probe("minute_kline", lambda: fetch_tencent_minute("600519", market="sh"), required=False),
        _probe("stock_fund_flow", lambda: fetch_stock_fund_flow("600519", market="sh"), required=False),
        _probe("sector_fund_flow", lambda: fetch_sector_fund_flow("半导体", name="半导体"), required=False),
        _probe("dragon_tiger", lambda: fetch_dragon_tiger_rows("600519", asof=date.today()), required=False),
        _probe("board_quotes", fetch_board_quotes, required=False),
        _probe("northbound_flow", fetch_northbound_flow, required=False),
        _concurrent_kline_probe(workers),
    ]
    required_failed = any(item["required"] and item["status"] != "ok" for item in probes)
    optional_failed = any(item["status"] != "ok" for item in probes)
    health = provider_health.summary()
    providers = health.get("providers") if isinstance(health, dict) else {}
    relevant = {
        key: providers.get(key)
        for key in (
            "akshare",
            "adata",
            "eastmoney_datacenter",
            "eastmoney_push2_degraded",
            "eastmoney_fund_flow_degraded",
            "eastmoney_kline",
            "tencent",
        )
        if isinstance(providers, dict) and providers.get(key)
    }
    return {
        "schema": "datasource_fallback_smoke_v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "error" if required_failed else "degraded" if optional_failed else "ok",
        "probes": probes,
        "provider_health": {
            "schema": health.get("schema") if isinstance(health, dict) else None,
            "generated_at": health.get("generated_at") if isinstance(health, dict) else None,
            "providers": relevant,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run(args.workers)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
