#!/usr/bin/env python3
"""
打板回测 — 引擎层（策略 / 成本 / IS-OOS 切分）
================================================
只处理「主板 10cm 涨停事件」universe，对两个假设产出逐事件净收益序列：
- H1：T+1 竞价 gap ∈ [-1%, +3%] 进场过滤器 vs 买入全部涨停票（control）。
- H2：T 日真竞价封(首次封板≤09:25) vs 盘中封 两组次日强度对比。

对照组（random_entry / simple_breakout / buy_hold）的收益由 run 层从数据层另行喂入，
本层不触网、不碰对照池，便于用合成事件表独立单测。

事件字段（数据层提供）：
  code, name, date(T), t_close(涨停价), t1_open, t1_close,
  first_seal(HHMMSS), lianban, seal_amount, float_mktcap, sector, is_st
"""

import os
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))
from tradeability import limit_pct  # noqa: E402

DEFAULT_COST = {"commission": 0.00025, "stamp": 0.0005, "slippage": 0.002}
GAP_WINDOW = (-1.0, 3.0)          # daban 二板弱转强竞价窗口
AUCTION_SEAL_MINUTES = 9 * 60 + 25  # 真集合竞价封板上限 09:25


def net_return(buy: float, sell: float, cost: Dict[str, float] = DEFAULT_COST) -> float:
    """单笔净收益：买入加佣金+滑点，卖出扣佣金+印花税+滑点。"""
    if buy is None or sell is None or buy <= 0:
        raise ValueError("buy/sell 价格非法")
    eff_buy = buy * (1 + cost["commission"] + cost["slippage"])
    eff_sell = sell * (1 - cost["commission"] - cost["stamp"] - cost["slippage"])
    return eff_sell / eff_buy - 1.0


def parse_seal_minutes(value: Any) -> Optional[int]:
    """'092500' / '09:25' / '0925' → 分钟数；非法返回 None。"""
    if value is None or value == "":
        return None
    text = str(value).strip().replace(":", "")
    if not text.isdigit():
        return None
    if len(text) >= 4:
        return int(text[:2]) * 60 + int(text[2:4])
    return None


def passes_universe(ev: Dict[str, Any]) -> bool:
    code = str(ev.get("code", "")).zfill(6)
    name = str(ev.get("name", ""))
    if not code.startswith(("00", "60")):
        return False
    if limit_pct(code, name) != 10.0:        # 排除 ST(5)/创业科创(20)/北交所(30)
        return False
    if ev.get("is_st"):
        return False
    for k in ("t_close", "t1_open", "t1_close"):
        v = ev.get(k)
        if v is None or v <= 0:
            return False
    return True


def filter_universe(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in events if passes_universe(e)]


def gap_pct(ev: Dict[str, Any]) -> float:
    return (ev["t1_open"] - ev["t_close"]) / ev["t_close"] * 100.0


def split_by_date(events: List[Dict[str, Any]], split_date: str
                  ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """date < split_date → IS（样本内）；date >= split_date → OOS（样本外）。"""
    is_set = [e for e in events if str(e.get("date", "")) < split_date]
    oos_set = [e for e in events if str(e.get("date", "")) >= split_date]
    return is_set, oos_set


# 持有窗口变体（report-all-variants）：
# - open_close：买 T+1 开、卖 T+1 收。保守可成交（一字封死也能次日开盘买），但切掉隔夜跳空。
# - board_overnight：买 T 收(涨停价/打板买在板上)、卖 T+1 收。含隔夜跳空——真打板经济学所在，
#   但隐含「能在板上成交」假设，一字封死实际买不进，须配合可成交性闸门看待。
HOLD_MODES = ("open_close", "board_overnight")


def _event_return(ev: Dict[str, Any], hold_mode: str, cost: Dict[str, float]) -> float:
    if hold_mode == "board_overnight":
        return net_return(ev["t_close"], ev["t1_close"], cost)
    return net_return(ev["t1_open"], ev["t1_close"], cost)


def strategy_returns(events: List[Dict[str, Any]],
                     predicate: Callable[[Dict[str, Any]], bool],
                     cost: Dict[str, float] = DEFAULT_COST,
                     hold_mode: str = "open_close") -> List[float]:
    """对 universe 内满足 predicate 的事件，按 hold_mode 计净收益列表。"""
    return [_event_return(e, hold_mode, cost) for e in filter_universe(events) if predicate(e)]


# ---- 假设谓词 ----
def is_h1_signal(ev: Dict[str, Any]) -> bool:
    return GAP_WINDOW[0] <= gap_pct(ev) <= GAP_WINDOW[1]


def is_auction_seal(ev: Dict[str, Any]) -> bool:
    m = parse_seal_minutes(ev.get("first_seal"))
    return m is not None and m <= AUCTION_SEAL_MINUTES


def is_intraday_seal(ev: Dict[str, Any]) -> bool:
    m = parse_seal_minutes(ev.get("first_seal"))
    return m is not None and m > AUCTION_SEAL_MINUTES


def split_returns(events: List[Dict[str, Any]], cost: Dict[str, float] = DEFAULT_COST,
                  hold_mode: str = "open_close") -> Dict[str, Dict[str, List[float]]]:
    """
    一次性算出两个假设在某事件集合上的收益序列（不切 IS/OOS，由调用方先切）。
    返回 {"h1": {"signal", "control"}, "h2": {"auction", "intraday"}}。
    """
    universe = filter_universe(events)
    return {
        "h1": {
            "signal": strategy_returns(universe, is_h1_signal, cost, hold_mode),
            "control": [_event_return(e, hold_mode, cost) for e in universe],
        },
        "h2": {
            "auction": strategy_returns(universe, is_auction_seal, cost, hold_mode),
            "intraday": strategy_returns(universe, is_intraday_seal, cost, hold_mode),
        },
    }
