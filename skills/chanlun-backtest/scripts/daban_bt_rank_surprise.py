#!/usr/bin/env python3
"""
S1 超预期（RankSurprise）回测接线 — 升级方案 §6.1 + §8.1(a)，NON-LIVE
=====================================================================
把 skills/common/rank_surprise.py 的纯信号接到既有打板回测事件表上：

  事件表(daban_bt_data v3) → 板块×交易日梯队记录 → rank_surprise.evaluate_universe
  → 命中集合 {(code, date)} → daban_bt_engine.strategy_returns（含 P5(a) 成交约束
  + 既有费用口径 DEFAULT_COST + T+1 口径的 hold_mode）

**不新增网络请求**：只读已固化的事件表 JSON。

字段映射（事件表 → 信号记录）：
  auction_strength   = ActualGap% = (t1_open − t_close)/t_close×100   —— 今日竞价强度
  prior_return_pct   = (t_close − t_prev_close)/t_prev_close×100      —— 昨日(T日)收益
  prior_strength     = prior_return_pct，并列时以封板时间早晚(tiebreak)分强弱
  board_height       = lianban                                        —— 连板高度
  volume_ratio       = 事件表的 volume_ratio / t1_volume_ratio 字段

量比口径缺口（诚实标注，不许拿日线量比冒充）：方案要求「09:45 前量比」，日线事件表
（v3/v4 都一样）只有全日 volume，二者不是一回事。v4 明确把 volume_ratio 标成
unavailable:needs_intraday_minute_bars 而不是给个代理值，本适配器同样**不造代理值**，
信号一律 unavailable(volume_ratio_missing)，在报告里如实计数。要跑出非零样本，必须先把
盘中 09:45 量比落进事件表（见 docs/event-schema-v4-2026-08.md）。

红线：S1 未在 strategy_registry 注册。本脚本只产出研究数字，不得写回任何实盘状态。
"""

from __future__ import annotations

import argparse
import json
import os
from statistics import mean, median
from typing import Any, Dict, List, Optional, Sequence

import skills.common  # noqa: F401  -- puts skills/common on sys.path

# 兄弟模块 daban_bt_engine 靠「脚本自身目录进 sys.path[0]」被找到（直接执行本文件时
# 由解释器保证）。这里不再自己插 sys.path —— 多一处 import-path 手术就多一条
# maintainability 债务，且 skills.common 已经接管了公共路径。
import execution_constraints as xc  # noqa: E402
import rank_surprise as rs  # noqa: E402
from daban_bt_engine import (  # noqa: E402
    DEFAULT_COST,
    HOLD_MODES,
    filter_universe,
    gap_pct,
    parse_seal_minutes,
    strategy_returns,
)

SCHEMA = "rank_surprise_backtest_v1"
DEFAULT_HOLD_MODE = "board_overnight"


def _float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def event_record(event: Dict[str, Any]) -> Dict[str, Any]:
    """单个回测事件 → S1 信号记录（缺字段留 None，由信号层 fail-closed）。"""
    t_close = _float(event.get("t_close"))
    t_prev = _float(event.get("t_prev_close"))
    prior_return = (
        (t_close - t_prev) / t_prev * 100.0
        if t_close is not None and t_prev not in (None, 0)
        else None
    )
    seal = parse_seal_minutes(event.get("first_seal"))
    return {
        "code": str(event.get("code") or "").zfill(6),
        "date": str(event.get("date") or ""),
        "sector": event.get("sector"),
        "auction_strength": gap_pct(event) if t_close else None,
        "prior_return_pct": prior_return,
        "prior_strength": prior_return,
        # 昨日强度并列（打板 universe 里 T 日几乎都是 +10%）时，封板越早越强 →
        # 取负分钟数当 tiebreak；缺封板时间的记录 tiebreak 归 0（不假装更强）。
        "prior_strength_tiebreak": -float(seal) if seal is not None else None,
        "board_height": event.get("lianban"),
        "volume_ratio": event.get("volume_ratio", event.get("t1_volume_ratio")),
        "volume_ratio_source": event.get("volume_ratio_source"),
    }


def event_records(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [event_record(event) for event in filter_universe(list(events))]


def _stats(returns: Sequence[float]) -> Dict[str, Any]:
    values = [float(r) for r in returns]
    if not values:
        return {"n": 0, "mean": None, "median": None, "win_rate": None, "total": None}
    wins = sum(1 for v in values if v > 0)
    return {
        "n": len(values),
        "mean": round(mean(values), 6),
        "median": round(median(values), 6),
        "win_rate": round(wins / len(values), 6),
        "total": round(sum(values), 6),
    }


def run(
    events: Sequence[Dict[str, Any]],
    *,
    market_state: Optional[Dict[str, Any]] = None,
    hold_mode: str = DEFAULT_HOLD_MODE,
    constraints_enabled: bool = True,
    cost: Optional[Dict[str, float]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """跑一轮 S1 回测。constraints_enabled=False 只用于反事实对照，绝非可执行口径。"""
    if hold_mode not in HOLD_MODES:
        raise ValueError(f"unknown hold_mode: {hold_mode}; allowed {HOLD_MODES}")
    all_events = list(events)
    records = event_records(all_events)
    results = rs.evaluate_universe(records, market_state=market_state, cfg=cfg)
    fired = rs.signal_codes(results)

    constraints = dict(xc.constraints_config())
    constraints["enabled"] = bool(constraints_enabled)

    def predicate(event: Dict[str, Any]) -> bool:
        key = (str(event.get("code") or "").zfill(6), str(event.get("date") or ""))
        return key in fired

    returns = strategy_returns(
        all_events, predicate, cost or DEFAULT_COST, hold_mode,
        config=constraints,
    )
    return {
        "schema": SCHEMA,
        "hold_mode": hold_mode,
        "execution_constraints_enabled": bool(constraints_enabled),
        "event_count": len(all_events),
        "universe_count": len(records),
        "signal_summary": rs.summarize(results),
        "signal_count": len(fired),
        "filled_count": len(returns),
        "returns": _stats(returns),
        "degraded": sorted({d for r in results for d in r.get("degraded") or []}),
        "registered_in_strategy_registry": False,
        "note": "S1 未过 research_gate/未注册，本结果只作研究观察，不得用于实盘排序或仓位",
    }


def counterfactual(
    events: Sequence[Dict[str, Any]],
    *,
    market_state: Optional[Dict[str, Any]] = None,
    hold_mode: str = DEFAULT_HOLD_MODE,
    cost: Optional[Dict[str, float]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """反事实对照（方案 §6.2 第 1 条）：关掉 P5 成交约束后收益虚高多少。

    差值为正且样本非空，才说明约束真的在咬；否则回测里的约束是装饰。
    """
    on = run(events, market_state=market_state, hold_mode=hold_mode,
             constraints_enabled=True, cost=cost, cfg=cfg)
    off = run(events, market_state=market_state, hold_mode=hold_mode,
              constraints_enabled=False, cost=cost, cfg=cfg)
    on_mean, off_mean = on["returns"]["mean"], off["returns"]["mean"]
    inflation = (
        round(off_mean - on_mean, 6)
        if on_mean is not None and off_mean is not None else None
    )
    return {
        "schema": SCHEMA,
        "hold_mode": hold_mode,
        "with_constraints": on,
        "without_constraints": off,
        "excluded_by_constraints": off["filled_count"] - on["filled_count"],
        "mean_return_inflation": inflation,
        "constraints_bite": bool(
            on["filled_count"] > 0
            and off["filled_count"] > on["filled_count"]
            and inflation is not None and inflation > 0
        ),
    }


def load_events(path: str) -> Dict[str, Any]:
    with open(os.path.abspath(os.path.expanduser(path)), encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        return {"schema": None, "events": payload}
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise ValueError(f"事件表格式不认识：{path}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="S1 超预期（RankSurprise）回测 — NON-LIVE 研究用，不触网"
    )
    parser.add_argument("--table", required=True, help="已固化事件表 JSON 路径")
    parser.add_argument("--hold-mode", default=DEFAULT_HOLD_MODE, choices=list(HOLD_MODES))
    parser.add_argument("--market-state", default=None,
                        help="市场/题材 S 状态（如 S3）；不给则题材条件 fail-closed")
    parser.add_argument("--counterfactual", action="store_true",
                        help="同时跑关闭成交约束的对照，报告收益虚高幅度")
    args = parser.parse_args()

    payload = load_events(args.table)
    state = ({"available": True, "dominant_state": args.market_state}
             if args.market_state else None)
    runner = counterfactual if args.counterfactual else run
    result = runner(payload["events"], market_state=state, hold_mode=args.hold_mode)
    result["source_table"] = args.table
    result["source_schema"] = payload.get("schema")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
