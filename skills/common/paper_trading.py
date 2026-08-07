"""Research-only paper broker for recommendation-gated Chanlun experiments.

The invariant is deliberate: an existing 09:35 recommendation must pass first;
Chanlun can only reject that recommendation, never source or re-rank a stock.
This module never sends orders and never writes the live ``portfolio_after``
projection key.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from a_share_rules import next_trading_day, t1_constraint
from execution_model import estimate_trade_cost
from portfolio_policy import evaluate_new_position, portfolio_value
from tradeability import assess_tradeability


ACCOUNT_SCHEMA = "paper_trading_account_v1"
GATE_SCHEMA = "paper_entry_gate_v1"
GATE_ORDER = [
    "open_recommendation",
    "open_confirmation",
    "chanlun_filter",
    "execution_checks",
]
SHANGHAI = ZoneInfo("Asia/Shanghai")

# 空仓归因（issue #174）：0 成交 + status=ok 时，运维面无法区分「上游门禁按设计
# 拒绝」与「数据面缺了」。这两类的处置完全相反——前者不需要人看，后者必须查。
# 未登记的 reason 一律归入需要人看，绝不静默当正常（新增拒绝原因时会自然暴露）。
DESIGNED_GATE_REASONS = frozenset({
    "recommendation_not_positive",
    "recommendation_score_below_threshold",
    "recommendation_quality_not_passed",
    "open_confirmation_not_positive",
    "chanlun_bullish_filter_not_met",
    "chanlun_bearish_veto",
})
GATE_PASSED_REASON = "recommendation_then_chanlun_passed"
MARKET_OR_ACCOUNT_REASONS = frozenset({
    "not_tradeable",
    "limit_queue_unobservable",
    "maximum_chase_price_exceeded",
    "duplicate_open_position",
    "max_positions_reached",
    "insufficient_cash_for_round_lot",
})


def classify_zero_fill(
    evaluations: Iterable[Mapping[str, Any]],
    *,
    filled: int = 0,
    reused: int = 0,
) -> dict[str, Any]:
    """把「今天为什么没成交」归到一类，并给出要不要人来看。

    - ``upstream_gate``：全部拒绝都来自设计内门禁，属正常 fail-closed；
    - ``market_or_account``：涨停买不进/仓位已满/现金不足一手等正常状态；
    - ``data_anomaly``：行情或执行面证据缺失，**只要出现一条就整批需要人看**；
    - ``no_candidates``：上游根本没给候选，同样需要人看；
    - ``None``：本批有成交或有幂等复用，不适用。

    只统计**被拒**的评估：门禁放行后成交或 reused 的记录带的是通过结论
    （``recommendation_then_chanlun_passed``），把它当拒绝原因会让每次幂等重跑
    都误报成数据异常。
    """
    reasons = [
        str((item or {}).get("reason") or "").strip()
        for item in evaluations
        if str((item or {}).get("reason") or "").strip() != GATE_PASSED_REASON
    ]
    reasons = [reason for reason in reasons if reason]
    breakdown: dict[str, int] = {}
    for reason in reasons:
        breakdown[reason] = breakdown.get(reason, 0) + 1
    anomaly = sorted({
        reason
        for reason in reasons
        if reason not in DESIGNED_GATE_REASONS
        and reason not in MARKET_OR_ACCOUNT_REASONS
    })

    if filled > 0 or reused > 0:
        zero_fill_class: str | None = None
    elif not reasons:
        zero_fill_class = "no_candidates"
    elif anomaly:
        zero_fill_class = "data_anomaly"
    elif any(reason in MARKET_OR_ACCOUNT_REASONS for reason in reasons):
        zero_fill_class = "market_or_account"
    else:
        zero_fill_class = "upstream_gate"

    return {
        "zero_fill_class": zero_fill_class,
        "actionable": zero_fill_class in {"data_anomaly", "no_candidates"},
        "breakdown": dict(sorted(breakdown.items())),
        "anomaly_reasons": anomaly,
    }


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith(("sh", "sz", "bj")):
        text = text[2:]
    return text.zfill(6)


def _local_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sector(candidate: Mapping[str, Any]) -> str:
    context = candidate.get("selection_context") or {}
    sector = context.get("sector") or {}
    industry = context.get("industry") or {}
    return str(
        candidate.get("sector")
        or candidate.get("industry")
        or sector.get("name")
        or industry.get("name")
        or ""
    ).strip()


def _signal_key(signal: Mapping[str, Any]) -> tuple[int, str]:
    try:
        index = int(signal.get("idx"))
    except (TypeError, ValueError):
        index = -1
    return index, str(signal.get("date") or "")


def _eligible_chanlun_signals(
    candidate: Mapping[str, Any], config: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    gate = config["entry_gate"]
    bullish_types = {str(item) for item in gate["bullish_chanlun_types"]}
    bearish_types = {str(item) for item in gate["bearish_chanlun_types"]}
    maximum_age = int(gate["max_signal_age_bars"])
    chanlun = ((candidate.get("research_evidence") or {}).get("chanlun") or {})
    signals = []
    for raw in chanlun.get("signals") or []:
        if not isinstance(raw, Mapping):
            continue
        try:
            age = int(raw.get("signal_age_bars"))
        except (TypeError, ValueError):
            continue
        if age < 0 or age > maximum_age:
            continue
        signals.append(dict(raw))
    return (
        [item for item in signals if str(item.get("type")) in bullish_types],
        [item for item in signals if str(item.get("type")) in bearish_types],
    )


def evaluate_entry_gate(
    candidate: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply recommendation first and Chanlun strictly as a downstream veto."""
    result = {
        "schema": GATE_SCHEMA,
        "allowed": False,
        "gate_order": list(GATE_ORDER),
        "code": _code(candidate.get("code")),
        "experimental": True,
        "live_policy_effect": "none",
    }
    gate = config["entry_gate"]
    decision = str(candidate.get("decision") or "").lower()
    if decision not in {str(item).lower() for item in gate["positive_recommendations"]}:
        return {**result, "reason": "recommendation_not_positive"}
    if _number(candidate.get("open_score"), -1.0) < float(gate["minimum_open_score"]):
        return {**result, "reason": "recommendation_score_below_threshold"}
    if (candidate.get("quality_report") or {}).get("status") != "passed":
        return {**result, "reason": "recommendation_quality_not_passed"}
    plan = candidate.get("execution_plan") or {}
    if str(plan.get("decision") or "").lower() not in {
        str(item).lower() for item in gate["positive_recommendations"]
    }:
        return {**result, "reason": "open_confirmation_not_positive"}

    bullish, bearish = _eligible_chanlun_signals(candidate, config)
    if not bullish:
        return {**result, "reason": "chanlun_bullish_filter_not_met"}
    newest_bullish = max(bullish, key=_signal_key)
    newer_bearish = [item for item in bearish if _signal_key(item) >= _signal_key(newest_bullish)]
    if newer_bearish:
        return {
            **result,
            "reason": "chanlun_bearish_veto",
            "chanlun": {"bullish": newest_bullish, "bearish": max(newer_bearish, key=_signal_key)},
        }
    controls = candidate.get("execution_controls") or {}
    if str(controls.get("status") or "") not in {"ready", "estimate_only"}:
        return {**result, "reason": "execution_checks_not_ready"}
    return {
        **result,
        "allowed": True,
        "reason": "recommendation_then_chanlun_passed",
        "chanlun": {"bullish": newest_bullish, "bearish": None},
    }


def validate_open_surface(
    payload: Mapping[str, Any],
    *,
    asof: str,
    observed_at: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    if payload.get("schema") != "open_confirmation_v3" or payload.get("status") != "ready":
        raise ValueError("open_confirmation_surface_invalid")
    if str(payload.get("asof") or "") != asof:
        raise ValueError("trading_date_mismatch")
    generated = _local_datetime(payload.get("generated_at"))
    observed = _local_datetime(observed_at)
    if generated.date().isoformat() != asof or observed.date().isoformat() != asof:
        raise ValueError("trading_date_mismatch")
    not_before = time.fromisoformat(str(config["execution"]["open_confirmation_not_before"]))
    if observed.timetz().replace(tzinfo=None) < not_before or observed < generated:
        raise ValueError("before_open_confirmation")
    snapshot = payload.get("input_snapshot") or {}
    if not snapshot.get("snapshot_id") or not snapshot.get("source_versions"):
        raise ValueError("input_snapshot_unverifiable")
    return {
        "status": "ready",
        "asof": asof,
        "generated_at": generated.isoformat(),
        "observed_at": observed.isoformat(),
        "signal_count": len(payload.get("signals") or []),
    }


def default_account(config: Mapping[str, Any]) -> dict[str, Any]:
    initial = round(float(config["account"]["initial_cash"]), 2)
    return {
        "schema": ACCOUNT_SCHEMA,
        "config_version": str(config["version"]),
        "initial_cash": initial,
        "cash": initial,
        "positions": [],
        "realized_pnl": 0.0,
        "fees_paid": 0.0,
        "trade_count": 0,
        "updated_at": None,
    }


def _fresh_quote(
    quote: Mapping[str, Any], *, observed_at: str, config: Mapping[str, Any]
) -> tuple[bool, str]:
    price = _number(quote.get("price"))
    if price <= 0 or _number(quote.get("volume")) <= 0:
        return False, "quote_unavailable"
    try:
        age = (_local_datetime(observed_at) - _local_datetime(quote.get("fetched_at"))).total_seconds()
    except (TypeError, ValueError):
        return False, "quote_timestamp_invalid"
    if age < 0:
        return False, "quote_future_dated"
    if age > float(config["execution"]["maximum_quote_age_seconds"]):
        return False, "quote_stale"
    return True, "fresh"


def _buy_price(price: float, config: Mapping[str, Any]) -> float:
    return round(price * (1 + float(config["execution"]["slippage_bps"]) / 10_000), 2)


def _sell_price(price: float, config: Mapping[str, Any]) -> float:
    return round(price * (1 - float(config["execution"]["slippage_bps"]) / 10_000), 2)


def _rejected_buy(
    state: Mapping[str, Any], reason: str, gate: Mapping[str, Any], **details: Any
) -> dict[str, Any]:
    return {
        "status": "rejected",
        "reason": reason,
        **details,
        "gate": dict(gate),
        "account": state,
    }


def _buy_terms(
    state: Mapping[str, Any],
    candidate: Mapping[str, Any],
    quote: Mapping[str, Any],
    *,
    asof: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    signal_price = _number(quote.get("price"))
    fill_price = _buy_price(signal_price, config)
    maximum = _number((candidate.get("execution_plan") or {}).get("max_chase_price"))
    if maximum > 0 and fill_price > maximum:
        return {"status": "rejected", "reason": "maximum_chase_price_exceeded"}
    position_pct = _number((candidate.get("execution_plan") or {}).get("position_pct"))
    if position_pct <= 0:
        return {"status": "rejected", "reason": "position_size_unavailable"}
    assets = portfolio_value(state)
    cash_buffer = assets * float(config["account"]["cash_buffer_pct"]) / 100
    budget = min(
        assets * position_pct / 100,
        max(0.0, _number(state.get("cash")) - cash_buffer),
    )
    lot_size = int(config["account"]["lot_size"])
    shares = int(budget // (fill_price * lot_size)) * lot_size
    fee: Mapping[str, Any] = {}
    gross = total = 0.0
    while shares > 0:
        gross = fill_price * shares
        fee = estimate_trade_cost("buy", gross, asof=asof)
        total = gross + _number(fee.get("total"))
        if total <= _number(state.get("cash")) and total <= budget:
            break
        shares -= lot_size
    if shares <= 0:
        return {"status": "rejected", "reason": "insufficient_cash_for_round_lot"}
    return {
        "status": "ready",
        "signal_price": signal_price,
        "fill_price": fill_price,
        "shares": shares,
        "gross": gross,
        "fee": fee,
        "total": total,
        "assets": assets,
    }


def _position_from_buy(
    candidate: Mapping[str, Any], gate: Mapping[str, Any], terms: Mapping[str, Any]
) -> dict[str, Any]:
    code = _code(candidate.get("code"))
    plan = candidate.get("execution_plan") or {}
    fill_price = _number(terms.get("fill_price"))
    return {
        "code": code,
        "name": str(candidate.get("name") or code),
        "shares": int(terms["shares"]),
        "average_cost": fill_price,
        "cost": fill_price,
        "buy_date": terms["asof"],
        "peak_price": fill_price,
        "sector": terms["sector"],
        "strategy_id": candidate.get("strategy_id"),
        "lane": plan.get("strategy_lane")
        or ("daban" if str(candidate.get("strategy_id") or "").startswith("daban") else "trend"),
        "stop_price": plan.get("stop_price"),
        "target_price": plan.get("target_price"),
        "target_price_2": plan.get("target_price_2"),
        "entry_evidence": {
            "recommendation": {
                "decision": candidate.get("decision"),
                "open_score": candidate.get("open_score"),
            },
            "chanlun": dict(gate["chanlun"]["bullish"]),
        },
    }


def simulate_buy(
    account: Mapping[str, Any],
    candidate: Mapping[str, Any],
    quote: Mapping[str, Any],
    *,
    asof: str,
    observed_at: str,
    config: Mapping[str, Any],
    risk: Mapping[str, Any],
) -> dict[str, Any]:
    gate = evaluate_entry_gate(candidate, config)
    if not gate["allowed"]:
        return _rejected_buy(deepcopy(account), str(gate["reason"]), gate)
    state = deepcopy(account)
    code = _code(candidate.get("code"))
    if any(_code(item.get("code")) == code for item in state.get("positions") or []):
        return _rejected_buy(state, "duplicate_open_position", gate)
    if len(state.get("positions") or []) >= int(config["account"]["max_positions"]):
        return _rejected_buy(state, "max_positions_reached", gate)
    fresh, reason = _fresh_quote(quote, observed_at=observed_at, config=config)
    if not fresh:
        return _rejected_buy(state, reason, gate)
    tradeability = assess_tradeability(dict(quote), code, str(candidate.get("name") or ""))
    if tradeability.get("tradeable") is not True:
        queue_reason = "limit_queue_unobservable" if tradeability.get("status") in {"limit_up", "limit_up_sealed"} else "not_tradeable"
        return _rejected_buy(state, queue_reason, gate, tradeability=tradeability)
    terms = _buy_terms(state, candidate, quote, asof=asof, config=config)
    if terms["status"] != "ready":
        return _rejected_buy(state, str(terms["reason"]), gate)
    sector = _sector(candidate)
    concentration = evaluate_new_position(
        state,
        code=code,
        sector=sector,
        proposed_position_pct=(
            _number(terms["total"]) / _number(terms["assets"]) * 100
            if _number(terms["assets"]) > 0
            else 0.0
        ),
        max_single_position_pct=float(risk["max_single_position_pct"]),
        max_sector_exposure_pct=float(risk["max_sector_exposure_pct"]),
    )
    if not concentration["allowed"]:
        return _rejected_buy(
            state, "portfolio_policy_blocked", gate, portfolio_policy=concentration
        )
    terms = {**terms, "asof": asof, "sector": sector}
    position = _position_from_buy(candidate, gate, terms)
    state.setdefault("positions", []).append(position)
    state["cash"] = round(_number(state.get("cash")) - _number(terms["total"]), 2)
    state["fees_paid"] = round(
        _number(state.get("fees_paid")) + _number(terms["fee"].get("total")), 2
    )
    state["trade_count"] = int(state.get("trade_count") or 0) + 1
    state["updated_at"] = _local_datetime(observed_at).isoformat()
    trade = {
        "side": "buy",
        "code": code,
        "name": position["name"],
        "trade_date": asof,
        "observed_at": state["updated_at"],
        "signal_price": terms["signal_price"],
        "fill_price": terms["fill_price"],
        "shares": terms["shares"],
        "gross_value": round(_number(terms["gross"]), 2),
        "fees": terms["fee"],
        "cash_after": state["cash"],
        "experimental": True,
        "live_order_sent": False,
    }
    return {"status": "filled", "reason": "recommendation_then_chanlun_passed", "gate": gate, "trade": trade, "account": state}


def _sessions_elapsed(acquired_on: str, asof: str) -> int:
    if asof <= acquired_on:
        return 0
    count = 0
    current = acquired_on
    while current < asof and count < 32:
        current = next_trading_day(current).isoformat()
        count += 1
    return count if current <= asof else max(0, count - 1)


def _exit_reason(position: Mapping[str, Any], price: float, risk: Mapping[str, Any], asof: str, time_stop_sessions: int) -> str | None:
    cost = _number(position.get("average_cost") or position.get("cost"))
    if cost <= 0:
        return "position_cost_invalid"
    stop_price = _number(position.get("stop_price"), cost * (1 + float(risk["stop_loss_pct"]) / 100))
    target_price = _number(position.get("target_price"), cost * (1 + float(risk["take_profit_pct"]) / 100))
    peak = max(_number(position.get("peak_price"), cost), price)
    if price <= stop_price:
        return "hard_stop"
    if peak > cost and (price / peak - 1) * 100 <= -float(risk["trailing_stop_pct"]):
        return "trailing_stop"
    if price >= target_price:
        return "take_profit"
    if position.get("lane") == "daban" and _sessions_elapsed(str(position.get("buy_date")), asof) >= time_stop_sessions:
        return "time_stop"
    return None


def _account_after_exit_check(
    state: Mapping[str, Any], positions: Sequence[Mapping[str, Any]], observed_at: str
) -> dict[str, Any]:
    account_after = deepcopy(state)
    account_after["positions"] = deepcopy(positions)
    account_after["updated_at"] = _local_datetime(observed_at).isoformat()
    return account_after


def _pending_exit_event(
    *,
    status: str,
    code: str,
    reason: str,
    state: Mapping[str, Any],
    remaining: Sequence[Mapping[str, Any]],
    observed_at: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "status": status,
        "code": code,
        "reason": reason,
        **details,
        "account_after": _account_after_exit_check(state, remaining, observed_at),
    }


def _fill_exit(
    state: dict[str, Any],
    position: Mapping[str, Any],
    *,
    code: str,
    price: float,
    reason: str,
    asof: str,
    observed_at: str,
    config: Mapping[str, Any],
    remaining: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    fill_price = _sell_price(price, config)
    shares = int(position.get("shares") or 0)
    gross = fill_price * shares
    fee = estimate_trade_cost("sell", gross, asof=asof)
    proceeds = gross - _number(fee.get("total"))
    cost_basis = _number(position.get("average_cost") or position.get("cost")) * shares
    realized = proceeds - cost_basis
    state["cash"] = round(_number(state.get("cash")) + proceeds, 2)
    state["fees_paid"] = round(
        _number(state.get("fees_paid")) + _number(fee.get("total")), 2
    )
    state["realized_pnl"] = round(_number(state.get("realized_pnl")) + realized, 2)
    state["trade_count"] = int(state.get("trade_count") or 0) + 1
    return {
        "status": "filled",
        "side": "sell",
        "code": code,
        "reason": reason,
        "trade_date": asof,
        "observed_at": _local_datetime(observed_at).isoformat(),
        "signal_price": price,
        "fill_price": fill_price,
        "shares": shares,
        "gross_value": round(gross, 2),
        "fees": fee,
        "realized_pnl": round(realized, 2),
        "live_order_sent": False,
        "account_after": _account_after_exit_check(state, remaining, observed_at),
    }


def simulate_exit_checks(
    account: Mapping[str, Any], quotes_by_code: Mapping[str, Mapping[str, Any]], *,
    asof: str, observed_at: str, config: Mapping[str, Any], risk: Mapping[str, Any],
    time_stop_sessions: int,
) -> dict[str, Any]:
    state = deepcopy(account)
    events: list[dict[str, Any]] = []
    kept = []
    positions = [dict(item) for item in state.get("positions") or []]
    for index, position in enumerate(positions):
        code = _code(position.get("code"))
        quote = quotes_by_code.get(code) or quotes_by_code.get(f"sh{code}") or quotes_by_code.get(f"sz{code}")
        if not isinstance(quote, Mapping):
            kept.append(position)
            events.append({"status": "blocked", "code": code, "reason": "quote_unavailable"})
            continue
        fresh, freshness_reason = _fresh_quote(quote, observed_at=observed_at, config=config)
        if not fresh:
            kept.append(position)
            events.append({"status": "blocked", "code": code, "reason": freshness_reason})
            continue
        price = _number(quote.get("price"))
        position["peak_price"] = max(_number(position.get("peak_price")), price)
        reason = (position.get("pending_exit") or {}).get("reason") or _exit_reason(position, price, risk, asof, time_stop_sessions)
        if not reason:
            kept.append(position)
            continue
        constraint = t1_constraint(position.get("buy_date"), asof)
        if not constraint["sell_allowed"]:
            position["pending_exit"] = {
                "reason": reason,
                "triggered_on": asof,
                "earliest_sell_date": constraint["earliest_sell_date"],
            }
            kept.append(position)
            events.append(
                _pending_exit_event(
                    status="pending_t1",
                    code=code,
                    reason=reason,
                    state=state,
                    remaining=kept + positions[index + 1 :],
                    observed_at=observed_at,
                    t1=constraint,
                )
            )
            continue
        tradeability = assess_tradeability(dict(quote), code, str(position.get("name") or ""))
        if tradeability.get("status") in {"halted", "limit_down"} or tradeability.get("tradeable") is False:
            position["pending_exit"] = {"reason": reason, "triggered_on": asof, "execution_block": tradeability.get("status")}
            kept.append(position)
            events.append(
                _pending_exit_event(
                    status="pending_unfilled",
                    code=code,
                    reason=reason,
                    state=state,
                    remaining=kept + positions[index + 1 :],
                    observed_at=observed_at,
                    tradeability=tradeability,
                )
            )
            continue
        events.append(
            _fill_exit(
                state,
                position,
                code=code,
                price=price,
                reason=reason,
                asof=asof,
                observed_at=observed_at,
                config=config,
                remaining=kept + positions[index + 1 :],
            )
        )
    state["positions"] = kept
    state["updated_at"] = _local_datetime(observed_at).isoformat()
    return {"status": "ok", "events": events, "account": state}


def mark_to_market(
    account: Mapping[str, Any],
    quotes_by_code: Mapping[str, Mapping[str, Any]],
    *,
    asof: str,
    observed_at: str,
) -> dict[str, Any]:
    missing = []
    market_value = 0.0
    positions = []
    for raw in account.get("positions") or []:
        position = dict(raw)
        code = _code(position.get("code"))
        quote = quotes_by_code.get(code) or quotes_by_code.get(f"sh{code}") or quotes_by_code.get(f"sz{code}")
        price = _number((quote or {}).get("price")) if isinstance(quote, Mapping) else 0.0
        if price <= 0:
            missing.append(code)
            continue
        value = price * int(position.get("shares") or 0)
        market_value += value
        positions.append({"code": code, "price": price, "market_value": round(value, 2)})
    if missing:
        return {"status": "blocked", "asof": asof, "observed_at": observed_at, "missing_quotes": sorted(missing), "nav": None}
    nav = round(_number(account.get("cash")) + market_value, 2)
    return {
        "status": "ok",
        "asof": asof,
        "observed_at": observed_at,
        "cash": round(_number(account.get("cash")), 2),
        "market_value": round(market_value, 2),
        "nav": nav,
        "return_pct": round((nav / _number(account.get("initial_cash"), nav) - 1) * 100, 4) if _number(account.get("initial_cash")) > 0 else None,
        "positions": positions,
    }


def account_from_events(events: Sequence[Mapping[str, Any]], config: Mapping[str, Any]) -> dict[str, Any]:
    latest = None
    latest_sequence = -1
    for event in events:
        payload = event.get("payload") or {}
        snapshot = payload.get("paper_account_after") if isinstance(payload, Mapping) else None
        if not isinstance(snapshot, Mapping):
            continue
        sequence = int(event.get("sequence") or 0)
        if sequence >= latest_sequence:
            latest = deepcopy(snapshot)
            latest_sequence = sequence
    return latest if isinstance(latest, dict) else default_account(config)
