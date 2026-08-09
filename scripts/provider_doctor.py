#!/usr/bin/env python3
"""Probe external market datasets and report endpoint-specific health."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date
from typing import Any, Callable

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import glob  # noqa: E402

from a_share_rules import previous_trading_day  # noqa: E402
from a_stock_http import load_hermes_env  # noqa: E402
from paths import cron_output_dir  # noqa: E402
from data_provider import (  # noqa: E402
    _next_serper_key,
    fetch_public_finance_news,
    fetch_serper_news,
)
from market_adapters import (  # noqa: E402
    fetch_a_share_daily_kline,
    fetch_a_share_spot,
    fetch_board_quotes,
    fetch_dragon_tiger_rows,
    fetch_hot_money_limitup_pool,
    fetch_industry_catalog_ths,
    fetch_northbound_flow,
    fetch_stock_fund_flow,
    fetch_tencent_quote,
)
import provider_health  # noqa: E402


def _limitup_probe():
    asof = previous_trading_day(date.today()).strftime("%Y%m%d")
    return fetch_hot_money_limitup_pool(asof)


def _serper_news_probe():
    load_hermes_env()
    key = _next_serper_key()
    if not key:
        raise RuntimeError("SERPER_API_KEY/SERPER_API_KEYS missing")
    return fetch_serper_news("A股 最新", key, 1).data


PROBES: dict[str, dict[str, Any]] = {
    "tencent_quote": {
        "provider": "tencent",
        "required": True,
        "call": lambda: fetch_tencent_quote(["sh000001"]),
    },
    "stock_fund_flow": {
        "provider": "akshare->adata->eastmoney_push2_degraded",
        "required": False,
        "call": lambda: fetch_stock_fund_flow("600519", market="sh"),
    },
    "daily_kline": {
        "provider": "akshare->adata->tencent->eastmoney_push2_degraded",
        "required": True,
        "call": lambda: fetch_a_share_daily_kline("600519", market="sh", days=5),
    },
    "board_quotes": {
        "provider": "akshare_ths->adata->eastmoney_push2_degraded",
        "required": False,
        "call": fetch_board_quotes,
    },
    "northbound_flow": {
        "provider": "akshare->eastmoney_kamt",
        "required": False,
        "call": fetch_northbound_flow,
    },
    "dragon_tiger": {
        "provider": "eastmoney_datacenter",
        "required": False,
        "call": lambda: fetch_dragon_tiger_rows("600519"),
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
    "a_share_spot": {
        "provider": "akshare_sina->adata->eastmoney_datacenter->eastmoney_push2_degraded",
        "required": False,
        "call": fetch_a_share_spot,
    },
    "serper_news": {
        "provider": "serper",
        "required": False,
        "call": _serper_news_probe,
    },
    "public_finance_news": {
        "provider": "sina+eastmoney",
        "required": False,
        "call": lambda: fetch_public_finance_news(2).data,
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


ORPHAN_LOCK_MIN_AGE_SECONDS = 10 * 60


def _scan_orphan_locks(
    output_dir: str | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """扫描 cron 输出目录，找出有 X.json.lock 但无 X.json 且 lock 陈旧的孤儿锁。

    对应被杀死的 cron job：只留下空 .lock 无 artifact（07-03 hot-money-context 事故）。
    """
    root = output_dir or cron_output_dir()
    ref = now if now is not None else time.time()
    orphans: list[dict[str, Any]] = []
    for lock_path in glob.glob(os.path.join(root, "*", "*.json.lock")):
        artifact_path = lock_path[: -len(".lock")]
        if os.path.exists(artifact_path):
            continue
        try:
            mtime = os.path.getmtime(lock_path)
        except OSError:
            continue
        if ref - mtime <= ORPHAN_LOCK_MIN_AGE_SECONDS:
            continue
        orphans.append({
            "job": os.path.basename(os.path.dirname(lock_path)),
            "run_file": os.path.basename(artifact_path),
            "mtime": round(mtime, 3),
            "age_seconds": round(ref - mtime, 1),
        })
    orphans.sort(key=lambda item: item["mtime"])
    return {
        "check": "artifact_integrity",
        "status": "error" if orphans else "ok",
        "orphan_lock_count": len(orphans),
        "orphan_locks": orphans,
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
    artifact_integrity = _scan_orphan_locks()
    required_failed = any(
        item["required"] and item["status"] != "ok"
        for item in datasets.values()
    )
    optional_failed = any(item["status"] != "ok" for item in datasets.values())
    integrity_failed = artifact_integrity["status"] != "ok"
    return {
        "schema": "a_stock_provider_health_v1",
        "status": (
            "error"
            if required_failed or integrity_failed
            else "degraded"
            if optional_failed
            else "ok"
        ),
        "datasets": datasets,
        "artifact_integrity": artifact_integrity,
        "slo_ledger": _slo_ledger_summary(),
    }


def _slo_ledger_summary() -> dict[str, Any]:
    """Rolling-window SLO/circuit snapshot from real production traffic.

    Distinct from the point-in-time probes above: this reflects breaker
    state accumulated by actual request traffic, not a fresh synthetic call.
    Never allowed to fail the probe script itself.
    """
    try:
        return provider_health.summary()
    except Exception as exc:  # noqa: BLE001
        return {"schema": "a_stock_provider_health_summary_v1", "status": "error", "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.parse_args()
    report = run_probes()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
