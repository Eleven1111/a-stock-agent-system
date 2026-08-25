#!/usr/bin/env python3
"""
回测成交约束模型 — 一字板 / 回封参与率 / 跌停承接量 / 滑点分档
==================================================================
为什么存在：允许「一字板也能按收盘价买入、跌停无量也能按跌停价卖出」的回测，
会让所有涨停接力类策略的收益变成假绿。本模块把四条真实约束做成可单测的纯函数：

  1. 一字涨停全日未开板 → 禁止买入成交（fill_ratio=0）；
  2. 回封买入          → 回封日成交额达阈值才成交，且单笔 ≤ 当日成交额的参与率上限；
  3. 跌停无承接量      → 禁止按跌停价卖出，顺延至次一可成交时点（与 T+1 叠加）；
  4. 滑点分档          → 常态 5-20bp、高波动 20-50bp；涨跌停事件走 1/2/3 而非固定滑点。

fail-closed 纪律：缺分钟量、缺成交额、缺昨收、制度未知 → 一律判不可成交，
绝不放行。缺数据放行正是「假绿」的主要来源。

涨跌停价一律经 a_share_rules.price_limit_rule 按日期取制度，禁止用今天的
涨跌幅回测历史（2020 年的创业板是 10cm）。

阈值单一事实源：config/daban_thresholds.yaml 的 execution_constraints 节
（缺失回退 daban_config.DEFAULTS，同值）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import daban_config as _cfg
from a_share_rules import price_limit_rule
from tradeability import round_limit

SCHEMA = "execution_constraint_v1"

_PRICE_EPS = 0.005  # 分档取整后的价格比较容差（元）


def constraints_config(path: Optional[str] = None) -> Dict[str, Any]:
    """取 execution_constraints 阈值（yaml 覆盖 DEFAULTS）。"""
    return _cfg.section("execution_constraints", path)


def _positive(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _bar_prices(bar: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """OHLC 全齐且为正才返回，否则 None（fail-closed）。"""
    out: Dict[str, float] = {}
    for field in ("open", "high", "low", "close"):
        value = _positive(bar.get(field))
        if value is None:
            return None
        out[field] = value
    return out


def limit_prices(
    *,
    code: str,
    asof: Any,
    prev_close: Any,
    is_st: bool = False,
    sessions_since_listing: Optional[int] = None,
) -> Dict[str, Any]:
    """按日期取制度算当日涨跌停价。制度未知/昨收缺失 → status=blocked。"""
    previous = _positive(prev_close)
    if previous is None:
        return {"status": "blocked", "reason": "prev_close_missing",
                "limit_up": None, "limit_down": None, "limit_pct": None}
    rule = price_limit_rule(
        code=str(code), asof=asof, is_st=bool(is_st),
        sessions_since_listing=sessions_since_listing,
    )
    if rule["status"] != "known":
        return {"status": "blocked", "reason": rule.get("reason", "rule_unknown"),
                "limit_up": None, "limit_down": None, "limit_pct": None}
    if rule["limit_pct"] is None:
        return {"status": "no_daily_limit", "reason": rule["reason"],
                "rule_id": rule.get("rule_id"),
                "limit_up": None, "limit_down": None, "limit_pct": None}
    pct = float(rule["limit_pct"])
    return {
        "status": "known",
        "reason": "rule_resolved",
        "rule_id": rule.get("rule_id"),
        "limit_pct": pct,
        "limit_up": round_limit(previous, pct, up=True),
        "limit_down": round_limit(previous, pct, up=False),
    }


def classify_limit_state(bar: Dict[str, Any], limits: Dict[str, Any]) -> str:
    """当日涨跌停形态：one_word_limit_up / resealed_limit_up / limit_up_open /
    one_word_limit_down / limit_down / normal / unknown。"""
    prices = _bar_prices(bar)
    if prices is None or limits.get("status") != "known":
        return "unknown"
    up = float(limits["limit_up"])
    down = float(limits["limit_down"])
    at_up = prices["close"] >= up - _PRICE_EPS
    at_down = prices["close"] <= down + _PRICE_EPS
    if at_up:
        sealed_all_day = prices["low"] >= up - _PRICE_EPS
        if sealed_all_day and prices["open"] >= up - _PRICE_EPS:
            return "one_word_limit_up"
        if prices["open"] >= up - _PRICE_EPS:
            return "resealed_limit_up"      # 开在板上、盘中炸板、尾盘回封
        return "limit_up_open"              # 盘中打上涨停（首次封板）
    if at_down:
        if prices["high"] <= down + _PRICE_EPS:
            return "one_word_limit_down"
        return "limit_down"
    return "normal"


def _amount(bar: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[float]:
    """当日成交额（元）。缺 amount 时用 volume × close × 每手股数兜底。

    腾讯 ifzq / 通达信日线的 volume 单位是「手」，A 股 1 手 = 100 股；漏掉这个
    100 会把成交额低估两个数量级，直接让参与率与承接量阈值失效。两者皆缺 → None
    （fail-closed）。
    """
    direct = _positive(bar.get("amount"))
    if direct is not None:
        return direct
    volume = _positive(bar.get("volume"))
    close = _positive(bar.get("close"))
    if volume is None or close is None:
        return None
    return volume * close * float(cfg.get("volume_lot_shares", 100.0))


def assess_buy_fill(
    bar: Dict[str, Any],
    *,
    code: str,
    asof: Any,
    prev_close: Any,
    order_amount: Optional[float] = None,
    is_st: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """买入侧成交约束：一字禁买 / 回封按参与率部分成交 / 常态按参与率封顶。

    返回 {filled, fill_ratio, fill_amount, reason, limit_state, ...}。
    fill_ratio ∈ [0,1]，0 表示完全买不进（事件应被回测剔除）。
    """
    cfg = config or constraints_config()
    limits = limit_prices(code=code, asof=asof, prev_close=prev_close, is_st=is_st)
    state = classify_limit_state(bar, limits)
    day_amount = _amount(bar, cfg)
    order = _positive(order_amount) or _positive(cfg.get("order_amount"))
    base = {"schema": SCHEMA, "side": "buy", "limit_state": state,
            "limit_up": limits.get("limit_up"), "day_amount": day_amount,
            "order_amount": order}

    if state == "unknown" or day_amount is None or order is None:
        return {**base, "filled": False, "fill_ratio": 0.0, "fill_amount": 0.0,
                "reason": "missing_data_fail_closed"}
    if state == "one_word_limit_up":
        # 全日一字封死：排队在封单后面，成交概率约等于 0。
        return {**base, "filled": False, "fill_ratio": 0.0, "fill_amount": 0.0,
                "reason": "one_word_limit_up_no_fill"}

    participation = float(cfg.get("max_participation_rate", 0.01))
    capacity = day_amount * participation

    if state in {"resealed_limit_up", "limit_up_open"}:
        min_reseal = float(cfg.get("reseal_min_amount", 0.0))
        if day_amount < min_reseal:
            # 回封后真实成交量不足：板上根本没换手，视为买不进。
            return {**base, "filled": False, "fill_ratio": 0.0, "fill_amount": 0.0,
                    "reason": "reseal_amount_below_threshold",
                    "reseal_min_amount": min_reseal}
        seal_share = float(cfg.get("reseal_participation_rate", participation))
        capacity = day_amount * seal_share

    fill_amount = min(order, capacity)
    ratio = fill_amount / order if order else 0.0
    if ratio <= 0:
        return {**base, "filled": False, "fill_ratio": 0.0, "fill_amount": 0.0,
                "reason": "capacity_exhausted"}
    return {**base, "filled": True, "fill_ratio": round(ratio, 6),
            "fill_amount": round(fill_amount, 4),
            "reason": "partial_fill" if ratio < 1.0 else "full_fill",
            "capacity": round(capacity, 4)}


def assess_sell_fill(
    bar: Dict[str, Any],
    *,
    code: str,
    asof: Any,
    prev_close: Any,
    is_st: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """卖出侧成交约束：跌停且承接量不足 → 拒卖顺延（defer=True）。"""
    cfg = config or constraints_config()
    limits = limit_prices(code=code, asof=asof, prev_close=prev_close, is_st=is_st)
    state = classify_limit_state(bar, limits)
    day_amount = _amount(bar, cfg)
    base = {"schema": SCHEMA, "side": "sell", "limit_state": state,
            "limit_down": limits.get("limit_down"), "day_amount": day_amount}

    if state == "unknown" or day_amount is None:
        return {**base, "filled": False, "defer": True,
                "reason": "missing_data_fail_closed"}
    if state == "one_word_limit_down":
        return {**base, "filled": False, "defer": True,
                "reason": "one_word_limit_down_no_bid"}
    if state == "limit_down":
        min_amount = float(cfg.get("limit_down_min_amount", 0.0))
        if day_amount < min_amount:
            # 跌停价上没有承接量，挂单排不到 → 顺延到次一可成交时点。
            return {**base, "filled": False, "defer": True,
                    "reason": "limit_down_insufficient_bid",
                    "limit_down_min_amount": min_amount}
    return {**base, "filled": True, "defer": False, "reason": "fill_at_close"}


def slippage_bps(
    bar: Dict[str, Any],
    *,
    code: str,
    asof: Any,
    prev_close: Any,
    is_st: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """滑点分档。涨跌停事件不返回固定滑点（由上面的约束模型接管）。

    档位按当日振幅 (high-low)/prev_close：低于 volatile_range_pct 为常态档，
    否则为高波动档。每档取区间上沿（保守），缺数据同样取高波动上沿。
    """
    cfg = config or constraints_config()
    normal_hi = float(cfg.get("slippage_bps_normal", 20.0))
    volatile_hi = float(cfg.get("slippage_bps_volatile", 50.0))
    limits = limit_prices(code=code, asof=asof, prev_close=prev_close, is_st=is_st)
    state = classify_limit_state(bar, limits)
    if state in {"one_word_limit_up", "resealed_limit_up", "limit_up_open",
                 "one_word_limit_down", "limit_down"}:
        return {"tier": "limit_event", "bps": None, "limit_state": state,
                "reason": "constraint_model_applies"}
    prices = _bar_prices(bar)
    previous = _positive(prev_close)
    if prices is None or previous is None:
        return {"tier": "volatile", "bps": volatile_hi, "limit_state": state,
                "reason": "missing_data_conservative"}
    day_range_pct = (prices["high"] - prices["low"]) / previous * 100.0
    threshold = float(cfg.get("volatile_range_pct", 6.0))
    if day_range_pct >= threshold:
        return {"tier": "volatile", "bps": volatile_hi, "limit_state": state,
                "range_pct": round(day_range_pct, 4), "reason": "high_volatility"}
    return {"tier": "normal", "bps": normal_hi, "limit_state": state,
            "range_pct": round(day_range_pct, 4), "reason": "normal_volatility"}
