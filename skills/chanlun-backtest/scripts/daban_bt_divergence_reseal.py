#!/usr/bin/env python3
"""
S2 龙头分歧回封（DivergenceReseal）回测接线 — 升级方案 §6.1 + §8.1(a)，NON-LIVE
=================================================================================
把 skills/common/divergence_reseal.py 的纯信号接到既有打板回测事件表上：

  事件表(daban_bt_data v4) → 板块×交易日候选池记录 → divergence_reseal.evaluate_universe
  → 命中集合 {(code, date)} → daban_bt_engine.strategy_returns（含 P5(a) 成交约束
  + 既有费用口径 DEFAULT_COST + T+1 口径的 hold_mode）

**不新增网络请求**：只读已固化的事件表 JSON。

字段映射（事件表 → 信号记录）与诚实缺口标注
--------------------------------------------
v4 事件表（EVENT_SCHEMA=daban_bt_event_table_v4）已补齐三组证据（见
docs/event-schema-v4-2026-08.md）：板块横截面聚合 sector_limitup_count /
sector_fast_board_count、由「炸板次数>0 时取最后封板时间」派生的 reseal_time、
从 K 线历史算的 turnover_baseline_median / _sample_days。

仍然缺的是 **pre_reseal_turnover_pct**（封板前累计换手，需分钟线）：akshare 的
换手率是全日口径，不等价，**不拿它冒充**。因此在真实 v4 表上 S2 的条件4 仍会
unavailable、命中为 0；这是数据缺口，不是"没有信号"。

适配器对这些字段只做同名/近义透传（v3 旧命名保留回退）；
带 sector 但不带其余字段的记录，仍然诚实地判 unavailable，不拿"事件表里有涨停
就算板块涨停家数"这种间接推断顶替真实聚合——间接推断算出的"家数"会包含非
候选池的其他票，口径不等价，比 unavailable 更危险。

红线：S2 未在 strategy_registry 注册。本脚本只产出研究数字，不得写回任何实盘状态。
"""

from __future__ import annotations

import argparse
import json
import os
from statistics import mean, median
from typing import Any, Dict, List, Optional, Sequence

import skills.common  # noqa: F401  -- puts skills/common on sys.path

# 兄弟模块 daban_bt_engine 靠「脚本自身目录进 sys.path[0]」被找到（直接执行本文件时
# 由解释器保证）。这里不再自己插 sys.path —— 多一处 import-path 手术就多一处
# maintainability 债务，且 skills.common 已经接管了公共路径。
import divergence_reseal as dr  # noqa: E402
import execution_constraints as xc  # noqa: E402
from daban_bt_engine import (  # noqa: E402
    DEFAULT_COST,
    HOLD_MODES,
    filter_universe,
    strategy_returns,
)

SCHEMA = "divergence_reseal_backtest_v1"
DEFAULT_HOLD_MODE = "board_overnight"


def _first(event: Dict[str, Any], *names: str) -> Any:
    """按名字顺序取第一个非 None 的值（新命名优先，旧命名回退）。"""
    for name in names:
        value = event.get(name)
        if value is not None:
            return value
    return None


def event_record(event: Dict[str, Any]) -> Dict[str, Any]:
    """单个回测事件 → S2 信号记录。

    v3 事件表没有板块横截面聚合/分钟级回封时刻/换手基准，本函数只做诚实透传
    （字段存在就映射，不存在留 None），缺失一律交给信号层 fail-closed 处理，
    绝不用同票其它字段拼凑代理值。
    """
    return {
        "code": str(event.get("code") or "").zfill(6),
        "date": str(event.get("date") or ""),
        "sector": event.get("sector"),
        # v4 事件表用 sector_limitup_count / sector_fast_board_count /
        # turnover_baseline_median 命名；这里只做**近义字段透传**（信号层判定一行未改），
        # 旧命名保留为回退，v3 时代固化的表继续可读。
        "sector_limit_up_count": _first(event, "sector_limitup_count",
                                        "sector_limit_up_count"),
        "sector_fast_seal_count": _first(event, "sector_fast_board_count",
                                         "sector_fast_seal_count"),
        "reseal_time": event.get("reseal_time"),
        "pre_reseal_turnover_pct": event.get("pre_reseal_turnover_pct"),
        "turnover_baseline_median_pct": _first(event, "turnover_baseline_median",
                                               "turnover_baseline_median_pct"),
        "turnover_baseline_sample_days": event.get("turnover_baseline_sample_days"),
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
    hold_mode: str = DEFAULT_HOLD_MODE,
    constraints_enabled: bool = True,
    cost: Optional[Dict[str, float]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """跑一轮 S2 回测。constraints_enabled=False 只用于反事实对照，绝非可执行口径。"""
    if hold_mode not in HOLD_MODES:
        raise ValueError(f"unknown hold_mode: {hold_mode}; allowed {HOLD_MODES}")
    all_events = list(events)
    records = event_records(all_events)
    results = dr.evaluate_universe(records, cfg=cfg)
    fired = dr.signal_codes(results)

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
        "signal_summary": dr.summarize(results),
        "signal_count": len(fired),
        "filled_count": len(returns),
        "returns": _stats(returns),
        "degraded": sorted({d for r in results for d in r.get("degraded") or []}),
        "registered_in_strategy_registry": False,
        "note": "S2 未过 research_gate/未注册，本结果只作研究观察，不得用于实盘排序或仓位",
    }


def counterfactual(
    events: Sequence[Dict[str, Any]],
    *,
    hold_mode: str = DEFAULT_HOLD_MODE,
    cost: Optional[Dict[str, float]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """反事实对照（方案 §6.2 第 1 条）：关掉 P5 成交约束后收益虚高多少。

    差值为正且样本非空，才说明约束真的在咬；否则回测里的约束是装饰。
    """
    on = run(events, hold_mode=hold_mode, constraints_enabled=True, cost=cost, cfg=cfg)
    off = run(events, hold_mode=hold_mode, constraints_enabled=False, cost=cost, cfg=cfg)
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
        description="S2 龙头分歧回封（DivergenceReseal）回测 — NON-LIVE 研究用，不触网"
    )
    parser.add_argument("--table", required=True, help="已固化事件表 JSON 路径")
    parser.add_argument("--hold-mode", default=DEFAULT_HOLD_MODE, choices=list(HOLD_MODES))
    parser.add_argument("--counterfactual", action="store_true",
                        help="同时跑关闭成交约束的对照，报告收益虚高幅度")
    args = parser.parse_args()

    payload = load_events(args.table)
    runner = counterfactual if args.counterfactual else run
    result = runner(payload["events"], hold_mode=args.hold_mode)
    result["source_table"] = args.table
    result["source_schema"] = payload.get("schema")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
