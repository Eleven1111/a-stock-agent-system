"""纯函数趋势持仓退出评估。

规则只产生建议事件，不执行交易。所有默认倍数和天数都是预注册的初值，
上线前必须通过网格/样本外验证（尤其是 ATR 倍数、时间止损和行业强弱阈值）。
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Dict, Mapping, Optional, Tuple


# Initial research registration only; values require grid validation before live use.
DEFAULT_RULES: Dict[str, Any] = {
    "side": "long",
    "initial_atr_multiple": 2.5,  # grid-validate the 2-3 ATR range
    "trailing_atr_multiple": 3.0,  # grid-validate ATR trailing distance
    "time_stop_days": 5,  # grid-validate the 5-day no-progress stop
    "hard_time_stop_days": 10,  # grid-validate the 10-day maximum hold
    "time_stop_min_return_pct": 0.0,
    "breakout_stop_enabled": True,
    "industry_rs_stop_enabled": True,
}


def _number(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if isfinite(result) else default


def _value(source: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in source and source.get(name) is not None:
            return source.get(name)
    return default


def _price(source: Mapping[str, Any]) -> Optional[float]:
    return _number(_value(source, "price", "close", "last_price"))


def _atr(source: Mapping[str, Any]) -> Optional[float]:
    value = _number(_value(source, "atr", "atr_14", "atr14", "entry_atr"))
    return value if value is not None and value > 0 else None


def _side(rules: Mapping[str, Any]) -> str:
    return "short" if str(rules.get("side", "long")).lower() == "short" else "long"


def _result(
    action: str,
    reason: str,
    *,
    initial_stop: Optional[float],
    trailing_stop: Optional[float],
    high_watermark: Optional[float],
    low_watermark: Optional[float],
) -> Dict[str, Any]:
    stop_values = [value for value in (initial_stop, trailing_stop) if value is not None]
    # The long-side evaluator uses the higher of the initial/trailing stops.
    # Short-side callers still receive both explicit stops; ``stop_price`` is
    # corrected by the evaluator below when the active side is short.
    stop_price = max(stop_values) if action and stop_values and initial_stop is not None else None
    return {
        "action": action,
        "reason": reason,
        "initial_stop": initial_stop,
        "trailing_stop": trailing_stop,
        "stop_price": stop_price,
        "high_watermark": high_watermark,
        "low_watermark": low_watermark,
    }


def _stop_levels(
    entry: Mapping[str, Any],
    current: Mapping[str, Any],
    rules: Mapping[str, Any],
    *,
    side: str,
    entry_price: float,
    current_price: float,
    entry_atr: float,
    current_atr: float,
) -> Tuple[Dict[str, Optional[float]], float]:
    """ATR stops plus the watermark anchoring the trail, and the price that tests them.

    The unused side's watermark stays ``None`` so a short position never reports
    a high watermark (and vice versa).
    """
    initial_multiple = _number(
        _value(rules, "initial_atr_multiple", "initial_stop_atr_multiple"), 2.5
    ) or 2.5
    trailing_multiple = _number(
        _value(rules, "trailing_atr_multiple", "trail_atr_multiple"), 3.0
    ) or 3.0
    prior_trailing = _number(_value(entry, "trailing_stop", "stop_price"))
    if side == "short":
        initial_stop = entry_price + initial_multiple * entry_atr
        prior_low = _number(_value(entry, "low_watermark", "lowest_since_entry"))
        observed_low = _number(_value(current, "low"), current_price) or current_price
        watermark = min(prior_low, observed_low) if prior_low is not None else min(entry_price, observed_low)
        candidate_trailing = watermark + trailing_multiple * current_atr
        trailing_stop = min(prior_trailing, candidate_trailing) if prior_trailing is not None else candidate_trailing
        trigger_price = _number(_value(current, "high"), current_price) or current_price
        watermarks = {"high_watermark": None, "low_watermark": watermark}
    else:
        initial_stop = entry_price - initial_multiple * entry_atr
        prior_high = _number(_value(entry, "high_watermark", "highest_since_entry"))
        observed_high = _number(_value(current, "high"), current_price) or current_price
        watermark = max(prior_high, observed_high) if prior_high is not None else max(entry_price, observed_high)
        candidate_trailing = watermark - trailing_multiple * current_atr
        trailing_stop = max(prior_trailing, candidate_trailing) if prior_trailing is not None else candidate_trailing
        trigger_price = _number(_value(current, "low"), current_price) or current_price
        watermarks = {"high_watermark": watermark, "low_watermark": None}
    return {"initial_stop": initial_stop, "trailing_stop": trailing_stop, **watermarks}, trigger_price


def _late_exit_reason(
    entry: Mapping[str, Any],
    current: Mapping[str, Any],
    rules: Mapping[str, Any],
    *,
    side: str,
    entry_price: float,
    current_price: float,
) -> Optional[str]:
    """Time, breakout and industry-strength exits, checked in that order."""
    holding_days = _number(_value(current, "holding_days", "days_held"), 0.0) or 0.0
    return_pct = (
        (current_price / entry_price - 1.0) * 100.0 if side == "long"
        else (entry_price / current_price - 1.0) * 100.0
    )
    soft_days = _number(rules.get("time_stop_days"), 5.0) or 5.0
    hard_days = _number(rules.get("hard_time_stop_days"), 10.0) or 10.0
    min_return = _number(rules.get("time_stop_min_return_pct"), 0.0) or 0.0
    if holding_days >= hard_days:
        return "time_stop_10d"
    if holding_days >= soft_days and return_pct <= min_return:
        return "time_stop_5d_no_progress"

    if bool(rules.get("breakout_stop_enabled", True)):
        breakout = _number(_value(entry, "breakout_level", "breakout_price"))
        if breakout is not None and ((side == "long" and current_price <= breakout)
                                     or (side == "short" and current_price >= breakout)):
            return "fell_back_below_breakout"

    if bool(rules.get("industry_rs_stop_enabled", True)):
        industry_rs = _number(_value(
            current, "industry_relative_strength", "industry_residual_momentum", "industry_rs",
        ))
        if industry_rs is not None and ((side == "long" and industry_rs < 0)
                                        or (side == "short" and industry_rs > 0)):
            return "industry_relative_strength_negative"
    return None


def evaluate_hold(
    entry: Mapping[str, Any],
    current: Mapping[str, Any],
    rule_params: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return ``hold`` or ``exit`` for a long (or explicitly short) position.

    ``entry`` may contain ``price``, ``atr``, ``breakout_level`` and prior
    ``high_watermark``/``trailing_stop``. ``current`` may contain ``price`` or
    OHLC fields, ``atr``, ``holding_days`` and industry-relative strength.
    Inputs are never mutated. A missing price/ATR fails closed to ``hold`` with
    an explicit data reason, rather than manufacturing a stop signal.
    """
    if not isinstance(entry, Mapping) or not isinstance(current, Mapping):
        raise TypeError("entry and current must be mappings")
    rules = dict(DEFAULT_RULES)
    rules.update(dict(rule_params or {}))
    side = _side(rules)
    entry_price = _price(entry)
    current_price = _price(current)
    entry_atr = _atr(entry)
    current_atr = _atr(current) or entry_atr
    if entry_price is None or current_price is None:
        return _result("hold", "missing_price_data", initial_stop=None, trailing_stop=None,
                       high_watermark=None, low_watermark=None)
    if entry_atr is None or current_atr is None:
        return _result("hold", "missing_atr_data", initial_stop=None, trailing_stop=None,
                       high_watermark=None, low_watermark=None)

    levels, trigger_price = _stop_levels(
        entry, current, rules,
        side=side, entry_price=entry_price,
        current_price=current_price, entry_atr=entry_atr, current_atr=current_atr,
    )
    breached = (
        trigger_price >= levels["initial_stop"] if side == "short"
        else trigger_price <= levels["initial_stop"]
    )
    if breached:
        return _result("exit", "initial_atr_stop", **levels)
    trailed = (
        trigger_price >= levels["trailing_stop"] if side == "short"
        else trigger_price <= levels["trailing_stop"]
    )
    if trailed:
        return _result("exit", "atr_trailing_stop", **levels)

    reason = _late_exit_reason(
        entry, current, rules,
        side=side, entry_price=entry_price, current_price=current_price,
    )
    if reason:
        return _result("exit", reason, **levels)
    return _result("hold", "hold", **levels)


__all__ = ["DEFAULT_RULES", "evaluate_hold"]
