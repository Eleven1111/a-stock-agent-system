from __future__ import annotations

import copy
from typing import Any

from tail_close_strategy import INPUT_SCHEMA, canonical_hash, load_tail_config


TRADING_DATE = "2026-07-28"


def config() -> dict[str, Any]:
    return copy.deepcopy(load_tail_config())


def _timed(payload: dict[str, Any], clock: str) -> dict[str, Any]:
    stamp = f"{TRADING_DATE}T{clock}+08:00"
    return {**payload, "event_time": stamp, "available_time": stamp}


def _minutes(code_offset: int = 0) -> list[dict[str, Any]]:
    rows = []
    for offset in range(20):
        minute = 30 + offset
        price = 10.20 + code_offset * 0.05 + offset * 0.005
        rows.append(
            _timed(
                {
                    "time": f"14:{minute:02d}",
                    "close": round(price, 4),
                    "vwap": 10.10 + code_offset * 0.05,
                    "volume": 20000 + offset * 100,
                },
                f"14:{minute:02d}:30",
            )
        )
    return rows


def _stock(code: str, sector_id: str, offset: int = 0) -> dict[str, Any]:
    price = 10.30 + offset * 0.05
    return _timed(
        {
            "code": code,
            "name": f"研究样本{code}",
            "sector_id": sector_id,
            "listing_days": 500,
            "median_amount_20d": 500_000_000,
            "amount": 600_000_000 + offset * 10_000_000,
            "tradeable": True,
            "event_gate_passed": True,
            "is_st": False,
            "price": price,
            "open": price - 0.20,
            "high": price + 0.05,
            "low": price - 0.60,
            "vwap": price - 0.15,
            "session_return": 0.04,
            "ma5": price - 0.02,
            "ma10": price - 0.08,
            "ma20": price - 0.08,
            "limit_up": False,
            "sell_side_visible": True,
            "ask_price": price + 0.01,
            "near_limit_down": False,
            "sector_relative_strength": 0.8 - offset * 0.1,
            "minute_rows": _minutes(offset),
        },
        "14:49:50",
    )


def bundle(*, prepare: bool = False) -> dict[str, Any]:
    cfg = config()
    cutoff = "14:34:50" if prepare else "14:49:50"
    market = _timed(
        {
            "context_status": "ready",
            "regime": "risk_on",
            "benchmark_return_1400_to_cutoff": 0.001,
            "advancer_ratio": 0.58,
        },
        cutoff,
    )
    sectors = [
        _timed(
            {
                "sector_id": f"S{index}",
                "valid_member_count": 5,
                "breadth": 0.70 - index * 0.02,
                "session_relative_return": 0.03 - index * 0.002,
                "tail_relative_return": 0.01 - index * 0.001,
                "persistence": 0.8 - index * 0.03,
                "liquidity_support": 0.7 - index * 0.02,
                "pit_amount": 3_000_000_000 - index * 100_000_000,
            },
            cutoff,
        )
        for index in range(5)
    ]
    stocks = [
        _stock("600001", "S0", 0),
        _stock("000001", "S1", 1),
        _stock("600002", "S2", 2),
    ]
    if prepare:
        for stock in stocks:
            stock["event_time"] = f"{TRADING_DATE}T14:34:40+08:00"
            stock["available_time"] = f"{TRADING_DATE}T14:34:40+08:00"
            stock["minute_rows"] = []
    snapshot_hash = "a" * 64
    return {
        "schema": INPUT_SCHEMA,
        "trading_date": TRADING_DATE,
        "run_id": "tail-close-prepare-run" if prepare else "tail-close-decision-run",
        "batch_id": f"tail-close-{TRADING_DATE}",
        "session_id": f"cn-a-{TRADING_DATE}",
        "prepare_run_id": "tail-close-prepare-run",
        "decision_mode": "replay",
        "snapshot_id": f"tail-close-{TRADING_DATE}-{cutoff}",
        "snapshot_hash": snapshot_hash,
        "snapshot_sealed_at": f"{TRADING_DATE}T{cutoff}+08:00",
        "source_id": "fixture",
        "source_version": "fixture-v1",
        "feature_version": "tail-close-features-v1",
        "feature_hash": "b" * 64,
        "config_hash": canonical_hash(cfg),
        "code_version": "test-commit",
        "source_clock_offset_seconds": 0,
        "security_capacity_by_code": {
            "600001": 100_000,
            "000001": 100_000,
            "600002": 100_000,
        },
        "shared_research_signals": [],
        "source_watermarks": {
            "fixture": {
                "complete": True,
                "coverage_asof": f"{TRADING_DATE}T{cutoff}+08:00",
                "provider_published_at": f"{TRADING_DATE}T{cutoff}+08:00",
            }
        },
        "market": market,
        "sectors": sectors,
        "stocks": stocks,
    }
