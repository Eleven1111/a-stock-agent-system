"""Conservative A-share execution, fee, capacity, and P&L scenarios."""

from __future__ import annotations

from datetime import date
from typing import Any


FEE_SCHEDULE = {
    "schema": "a_share_fee_schedule_v1",
    "version": "cn-a-share-2023-08-28-estimate-v1",
    "effective_date": "2023-08-28",
    "source": "PRC stamp-duty effective date plus configurable broker-fee estimate",
    "commission_bps": 3.0,
    "minimum_commission": 5.0,
    "sell_stamp_duty_bps": 5.0,
    "transfer_fee_bps": 0.1,
    "authoritative_source": "broker_statement",
}


def fee_schedule_for(asof: str) -> dict[str, Any]:
    """Return a versioned schedule only inside its known effective period."""
    requested = date.fromisoformat(str(asof))
    effective = date.fromisoformat(FEE_SCHEDULE["effective_date"])
    if requested < effective:
        raise ValueError("fee_schedule_unknown")
    return dict(FEE_SCHEDULE)


def estimate_trade_cost(side: str, gross_value: float, *, asof: str) -> dict[str, Any]:
    """Estimate one side's fees; broker statements remain authoritative."""
    if side not in {"buy", "sell"} or gross_value <= 0:
        raise ValueError("side and positive gross_value are required")
    rules = fee_schedule_for(asof)
    commission = max(
        float(rules["minimum_commission"]),
        gross_value * float(rules["commission_bps"]) / 10_000,
    )
    stamp_duty = (
        gross_value * float(rules["sell_stamp_duty_bps"]) / 10_000
        if side == "sell"
        else 0.0
    )
    transfer_fee = gross_value * float(rules["transfer_fee_bps"]) / 10_000
    total = commission + stamp_duty + transfer_fee
    return {
        "side": side,
        "gross_value": round(gross_value, 4),
        "commission": round(commission, 4),
        "stamp_duty": round(stamp_duty, 4),
        "transfer_fee": round(transfer_fee, 4),
        "total": round(total, 4),
        "status": "estimate_only",
        "rules": rules,
        "authoritative_source": "broker_statement",
    }


def estimate_round_trip_pnl(
    *,
    entry_price: float,
    exit_price: float,
    quantity: int,
    asof: str,
    corporate_action_status: str = "clear",
) -> dict[str, Any]:
    """Estimate net P&L with both-side costs and reconciliation flags."""
    if entry_price <= 0 or exit_price <= 0 or quantity <= 0:
        raise ValueError("positive prices and quantity are required")
    entry_value = entry_price * quantity
    exit_value = exit_price * quantity
    buy_cost = estimate_trade_cost("buy", entry_value, asof=asof)
    sell_cost = estimate_trade_cost("sell", exit_value, asof=asof)
    total_cost = float(buy_cost["total"]) + float(sell_cost["total"])
    corporate_action_uncertain = corporate_action_status != "clear"
    return {
        "schema": "a_share_pnl_estimate_v1",
        "status": "estimate_only",
        "gross_pnl": round(exit_value - entry_value, 4),
        "estimated_cost": round(total_cost, 4),
        "estimated_net_pnl": round(exit_value - entry_value - total_cost, 4),
        "corporate_action_status": corporate_action_status,
        "reconciliation_required": corporate_action_uncertain,
        "authoritative_source": "broker_statement",
        "fee_schedule_version": buy_cost["rules"]["version"],
    }


def _capacity(quantity: int, signal_price: float, adv_value: float | None) -> dict[str, Any]:
    if adv_value is None or adv_value <= 0:
        return {"status": "capacity_unknown", "participation": None}
    participation = quantity * signal_price / adv_value
    return {"status": "estimated", "participation": round(participation, 8)}


def build_execution_scenarios(
    *,
    side: str,
    quantity: int,
    signal_price: float,
    limit_queue: bool,
    executable_price: float | None,
    available_volume: float | None,
    adv_value: float | None,
    event_asof: str,
) -> dict[str, Any]:
    """Separate theoretical signal, evidence-conditional fill, and conservative fill."""
    if side not in {"buy", "sell"} or quantity <= 0 or signal_price <= 0:
        raise ValueError("valid side, quantity, and signal_price are required")
    can_fill = (
        executable_price is not None
        and executable_price > 0
        and available_volume is not None
        and available_volume >= quantity
    )
    conditional = {
        "status": "filled" if can_fill else "unknown",
        "price": executable_price if can_fill else None,
        "requires": "executable_price_and_available_volume",
    }
    conservative = {
        "status": "unfilled" if limit_queue or not can_fill else "filled",
        "price": None if limit_queue or not can_fill else executable_price,
        "reason": "limit_queue_unobservable" if limit_queue else "fill_evidence",
    }
    return {
        "schema": "execution_scenarios_v1",
        "event_asof": event_asof,
        "signal": {"status": "signal_only", "price": signal_price},
        "conditional_fill": conditional,
        "conservative": conservative,
        "capacity": _capacity(quantity, signal_price, adv_value),
        "fee_schedule": fee_schedule_for(event_asof),
        "broker_authoritative": True,
    }
