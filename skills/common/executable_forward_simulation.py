#!/usr/bin/env python3
"""Turn a frozen forward prediction into a result an account could have had.

The settled forward label (``strategy_forward_settlement``) buys at the next
session's open and sells at the close of the horizon-th session.  At the primary
horizon of 1 that is a same-session round trip, which a cash A-share account
cannot do.  This module answers the other question: given the same frozen
signal, what happens once the buy has to clear a fill model and the sell has to
wait for T+1 measured from the session the buy actually happened on?

Nothing here re-implements costs, limit bands or fill capacity: it composes
``execution_constraints`` and ``execution_model``, the same modules the paper
engine uses.  Rejections and unresolved cases are returned, never dropped -- a
strategy that is only measurable on the subset it could enter is not measured.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from execution_constraints import assess_buy_fill, assess_sell_fill, constraints_config
from execution_model import net_return_pct

SCHEMA = "executable_forward_result_v1"
ENGINE_VERSION = "executable-forward-simulation-v1"

STATUS_EXITED = "exited"
STATUS_NOT_FILLED = "not_filled"
STATUS_UNRESOLVED = "unresolved_right_censored"
STATUS_PENDING = "pending_evidence"


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _sessions_after(bars: Sequence[Mapping[str, Any]], decision_date: str) -> list[dict[str, Any]]:
    return sorted(
        (dict(bar) for bar in bars if str(bar.get("trading_date") or "") > decision_date),
        key=lambda bar: str(bar["trading_date"]),
    )


def simulate_executable_forward(
    prediction: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    hold_sessions: int = 1,
    order_amount: float | None = None,
    prev_close: Any = None,
    is_st: bool = False,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Simulate one frozen prediction under real entry and T+1 exit constraints.

    ``hold_sessions`` counts sessions *held*, so the minimum of 1 already means
    "sell no earlier than the session after the buy".  A label horizon of 1 is
    not the same thing and must not be passed through as one.
    """

    if hold_sessions < 1:
        raise ValueError("hold_sessions must be at least 1: T+1 forbids a same-session exit")
    cfg = dict(config or constraints_config())
    code = str(prediction["entity_id"])
    decision_date = str(prediction["decision_date"])
    forward = _sessions_after(bars, decision_date)
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "engine_version": ENGINE_VERSION,
        "label_kind": "executable_simulated_result",
        "decision_id": prediction.get("decision_id"),
        "strategy_id": prediction.get("strategy_id"),
        "entity_id": code,
        "decision_date": decision_date,
        "signal_available_at": prediction.get("observed_at"),
        "hold_sessions": hold_sessions,
        "exit_rule": f"close_of_session_{hold_sessions}_after_the_fill",
        "respects_t_plus_one_from_entry": True,
    }
    if not forward:
        return {**base, "status": STATUS_PENDING, "reason": "no_session_after_decision"}

    entry_bar = forward[0]
    entry_day = str(entry_bar["trading_date"])
    if str(prediction.get("observed_at") or "")[:10] >= entry_day:
        return {
            **base, "status": STATUS_PENDING, "entry_date": entry_day,
            "reason": "signal_not_available_before_entry_session",
        }
    buy = assess_buy_fill(
        entry_bar, code=code, asof=entry_day,
        prev_close=prev_close if prev_close is not None else entry_bar.get("prev_close"),
        order_amount=order_amount, is_st=is_st, config=cfg,
    )
    base["entry_date"] = entry_day
    base["entry_assessment"] = buy
    if not buy.get("filled"):
        # Kept, not discarded: an entry that could not be taken is part of the
        # denominator, otherwise the strategy is scored on its easy days only.
        return {**base, "status": STATUS_NOT_FILLED, "reason": buy.get("reason")}

    entry_price = _positive(entry_bar.get("open"))
    if entry_price is None:
        return {**base, "status": STATUS_PENDING, "reason": "entry_open_missing"}

    return _resolve_exit(base, forward, entry_price, buy, code, hold_sessions, is_st, cfg)


def _resolve_exit(
    base: dict[str, Any],
    forward: Sequence[Mapping[str, Any]],
    entry_price: float,
    buy: Mapping[str, Any],
    code: str,
    hold_sessions: int,
    is_st: bool,
    cfg: Mapping[str, Any],
) -> dict[str, Any]:
    """Walk forward from the earliest T+1-legal session until a sell clears."""

    deferrals: list[dict[str, Any]] = []
    for offset in range(hold_sessions, len(forward)):
        candidate = forward[offset]
        sell = assess_sell_fill(
            candidate, code=code, asof=str(candidate["trading_date"]),
            prev_close=forward[offset - 1].get("close"), is_st=is_st, config=dict(cfg),
        )
        if not sell.get("filled"):
            deferrals.append({
                "session": str(candidate["trading_date"]), "reason": sell.get("reason"),
            })
            continue
        exit_price = _positive(candidate.get("close"))
        if exit_price is None:
            deferrals.append({
                "session": str(candidate["trading_date"]), "reason": "exit_close_missing",
            })
            continue
        gross = exit_price / entry_price - 1
        priced = net_return_pct(
            gross_return_pct=gross * 100,
            notional=float(buy.get("fill_amount") or cfg.get("order_amount") or 0.0) or None,
            asof=str(candidate["trading_date"]),
        )
        return {
            **base,
            "status": STATUS_EXITED,
            "entry_price": entry_price,
            "exit_date": str(candidate["trading_date"]),
            "exit_price": exit_price,
            "sessions_held": offset,
            "days_blocked": len(deferrals),
            "deferrals": deferrals,
            "gross_return": gross,
            "net_return": float(priced["net_return_pct"]) / 100,
            "cost_model": priced,
            "exit_assessment": sell,
            "execution_evidence": True,
        }
    return {
        **base,
        "status": STATUS_UNRESOLVED,
        "entry_price": entry_price,
        "days_blocked": len(deferrals),
        "deferrals": deferrals,
        "reason": "no_sellable_session_within_available_bars",
        "execution_evidence": False,
    }


__all__ = [
    "ENGINE_VERSION", "SCHEMA", "STATUS_EXITED", "STATUS_NOT_FILLED",
    "STATUS_PENDING", "STATUS_UNRESOLVED", "simulate_executable_forward",
]
