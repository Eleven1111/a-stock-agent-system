"""Live trading-discipline circuit breaker computed from the real signal ledger.

config/daban_thresholds.yaml already declares a market_gate section (week trade
cap, day/week loss stop, consecutive-loss freeze), and daban_candidate_api.py
already checks it — but only against caller-supplied numbers. Nothing in the
live pipeline ever computed those numbers from real trades, so the circuit
breaker never actually tripped. This module closes that gap: pure function,
reads trade.executed events already written by portfolio_manager, no network.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping, Optional, Sequence

import daban_config

SCHEMA = "trading_discipline_v1"


def _parse_date(value: Any):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def assess_discipline_state(
    events: Sequence[Mapping[str, Any]],
    *,
    total_assets: float,
    asof: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """从真实 trade.executed 事件算出周开仓数/日周亏损/连续错单，对照 market_gate 阈值。"""
    cfg = dict(config or daban_config.section("market_gate"))
    asof_date = _parse_date(asof) or date.today()
    week_start = _week_start(asof_date)

    trades = [
        dict(event.get("payload") or {})
        for event in (events or [])
        if str(event.get("event_type")) == "trade.executed"
    ]

    opens_this_week = [
        trade for trade in trades
        if str(trade.get("action")) in {"open", "add"}
        and (d := _parse_date(trade.get("trade_date"))) is not None
        and week_start <= d <= asof_date
    ]

    closes = sorted(
        (
            trade for trade in trades
            if str(trade.get("action")) == "close" and trade.get("trade_date")
        ),
        key=lambda trade: trade["trade_date"],
    )
    closes_today = [t for t in closes if t.get("trade_date") == asof_date.isoformat()]
    closes_this_week = [
        t for t in closes
        if (d := _parse_date(t.get("trade_date"))) is not None and week_start <= d <= asof_date
    ]

    assets = float(total_assets or 0)
    day_pnl = sum(float(t.get("pnl") or 0) for t in closes_today)
    week_pnl = sum(float(t.get("pnl") or 0) for t in closes_this_week)
    day_loss_pct = round(day_pnl / assets * 100, 2) if assets > 0 else 0.0
    week_loss_pct = round(week_pnl / assets * 100, 2) if assets > 0 else 0.0

    consecutive_losses = 0
    for trade in reversed(closes):
        pnl = trade.get("pnl")
        if pnl is None or float(pnl) >= 0:
            break
        consecutive_losses += 1

    reasons: list[str] = []
    if len(opens_this_week) >= int(cfg["week_trades_max"]):
        reasons.append("week_trade_cap")
    if day_loss_pct <= float(cfg["day_loss_pct_stop"]):
        reasons.append("day_loss_stop")
    if week_loss_pct <= float(cfg["week_loss_pct_freeze"]):
        reasons.append("week_loss_freeze")
    if consecutive_losses >= int(cfg["consecutive_losses_max"]):
        reasons.append("consecutive_losses_freeze")

    return {
        "schema": SCHEMA,
        "asof": asof_date.isoformat(),
        "week_trades": len(opens_this_week),
        "day_loss_pct": day_loss_pct,
        "week_loss_pct": week_loss_pct,
        "consecutive_losses": consecutive_losses,
        "blocked": bool(reasons),
        "reasons": reasons,
        "thresholds": cfg,
    }


if __name__ == "__main__":
    import json

    import portfolio_policy
    import signal_ledger
    from paths import data_file
    from state_store import read_json

    portfolio = read_json(data_file("stock-triage", "portfolio.json"), {})
    print(json.dumps(
        assess_discipline_state(
            signal_ledger.read_events(),
            total_assets=portfolio_policy.portfolio_value(portfolio),
        ),
        ensure_ascii=False,
        indent=2,
    ))
