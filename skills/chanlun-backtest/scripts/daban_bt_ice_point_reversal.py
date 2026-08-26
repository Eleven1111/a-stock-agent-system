#!/usr/bin/env python3
"""
S6 冰点反转（IcePointReversal）回测接线 — 升级方案 §6.1，NON-LIVE
=======================================================================
把 skills/common/ice_point_reversal.py 的纯信号接到既有打板回测事件表上：

  事件表(daban_bt_data v4) → 单事件记录（无需题材分组——四项合取全部是
  市场级状态，同一天所有候选共享同一份证据）→ ice_point_reversal.
  evaluate_universe → 命中集合 {(code, date)} → daban_bt_engine.
  strategy_returns（含 P5(a) 成交约束 + 既有费用口径 DEFAULT_COST +
  T+1 口径的 hold_mode）

**不新增网络请求**：只读已固化的事件表 JSON + 可选的情绪日报序列 JSON。

字段映射（事件表 → 信号记录）与诚实缺口标注
--------------------------------------------
既有 daban_bt_data(v3/v4) 事件表是"单日涨停快照"结构，S6 需要的四项证据全部是
**市场级、跨交易日**的证据，事件表结构上不携带：

  - sentiment_series（S_t/ΔS_t 计算输入）：需要至少 180 个交易日的
    sentiment_daily 时间序列（skills/common/sentiment_daily.py 产出），
    事件表逐日独立快照，不落这条序列。本脚本开放 --sentiment-table 参数，
    读入后按 trading_date <= 事件日 切片喂给 ice_point_reversal（S_t 是
    市场级统计量，同一天的切片对所有候选相同）；不给该参数则该项证据整体
    None，交 evaluate() 按 fail-closed 规则判 unavailable。
  - leader_confirm（逆势活口是否被市场确认）：需要跨标的的"新出现的反弹
    龙头是否被市场追认"这一判断，本机没有对应管道产出这个布尔量，事件表
    结构上也不携带——同 S1 的 theme_alive、S5 的
    was_prior_period_top_leader 缺口同构。本脚本开放 --leader-confirm
    参数透传（true/false），不给则 fail-closed。
  - sector_breadth_top（板块扩散广度）：sentiment_daily 已经产出
    sector_breadth_top 这个派生字段（见 skills/common/sentiment_daily.py
    的 sector_breadth_top()），若 --sentiment-table 提供的记录里最新一条
    带有该字段，本脚本会读取；否则 None。

因此**没有全部三类证据同时非空**的真实事件表可跑，零命中是数据缺口，不是
"策略没有信号"——三处任一为 None，evaluate() 就会判 unavailable。

前置依赖未满足（升级方案 §6.1 明确要求）：S6"依赖 P1 校准结论支持后才启动
回测"。P1（State PnL 分阶段收益归因，#269）在本机是**零样本 UNVERIFIED**——
情绪状态是否真有区分度既未证实也未证伪。本脚本只交付管道，不构成"P1 前置
已满足"的证据，注册条件比其他策略更严：需 P1 先在 full 模式下产出覆盖样本
且分档单格 n>=30。

红线：S6 未在 strategy_registry 注册。本脚本只产出研究数字，不得写回任何
实盘状态。
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
import execution_constraints as xc  # noqa: E402
import ice_point_reversal as ipr  # noqa: E402
from daban_bt_engine import (  # noqa: E402
    DEFAULT_COST,
    HOLD_MODES,
    filter_universe,
    strategy_returns,
)

SCHEMA = "ice_point_reversal_backtest_v1"
DEFAULT_HOLD_MODE = "board_overnight"


def event_record(event: Dict[str, Any]) -> Dict[str, Any]:
    """单个回测事件 → S6 信号记录（只携带 code/date，市场级证据走 market_state）。"""
    return {"code": str(event.get("code") or "").zfill(6), "date": str(event.get("date") or "")}


def event_records(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [event_record(event) for event in filter_universe(list(events))]


def build_market_state(
    sentiment_records: Optional[Sequence[Dict[str, Any]]],
    *,
    as_of_date: Optional[str] = None,
    leader_confirm: Optional[bool] = None,
) -> Dict[str, Any]:
    """把情绪日报序列切到 ``as_of_date``（含）为止，构造 S6 需要的 market_state。

    ``sentiment_series`` 是市场级统计量，同一天对所有候选相同——切片只按
    trading_date 排序截断，不做任何补值/近似。``sector_breadth_top`` 取切片
    末条记录自带的同名字段（sentiment_daily 已产出，本脚本不重算）。
    """
    rows = sorted(
        (dict(r) for r in sentiment_records or []),
        key=lambda r: str(r.get("trading_date") or ""),
    )
    if as_of_date is not None:
        rows = [r for r in rows if str(r.get("trading_date") or "") <= str(as_of_date)]
    breadth = rows[-1].get("sector_breadth_top") if rows else None
    return {
        "sentiment_series": rows,
        "leader_confirm": leader_confirm,
        "sector_breadth_top": breadth,
    }


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
    sentiment_records: Optional[Sequence[Dict[str, Any]]] = None,
    leader_confirm: Optional[bool] = None,
    hold_mode: str = DEFAULT_HOLD_MODE,
    constraints_enabled: bool = True,
    cost: Optional[Dict[str, float]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """跑一轮 S6 回测。constraints_enabled=False 只用于反事实对照，绝非可执行口径。

    每个事件按自己的日期切片情绪序列（S_t 不得看到事件日之后的情绪数据——
    反未来函数纪律，同 S1-S5）。事件按日期分组，同日事件共享同一份
    market_state（避免逐条重复排序切片）。
    """
    if hold_mode not in HOLD_MODES:
        raise ValueError(f"unknown hold_mode: {hold_mode}; allowed {HOLD_MODES}")
    all_events = list(events)
    universe = filter_universe(all_events)

    by_date: Dict[str, list] = {}
    for event in universe:
        by_date.setdefault(str(event.get("date") or ""), []).append(event)

    results: List[Dict[str, Any]] = []
    for event_date, day_events in by_date.items():
        state = build_market_state(
            sentiment_records, as_of_date=event_date, leader_confirm=leader_confirm,
        )
        day_records = [event_record(e) for e in day_events]
        results.extend(ipr.evaluate_universe(day_records, market_state=state, cfg=cfg))
    fired = ipr.signal_codes(results)

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
        "universe_count": len(universe),
        "signal_summary": ipr.summarize(results),
        "signal_count": len(fired),
        "filled_count": len(returns),
        "returns": _stats(returns),
        "degraded": sorted({d for r in results for d in r.get("degraded") or []}),
        "registered_in_strategy_registry": False,
        "note": (
            "S6 未过 research_gate/未注册，本结果只作研究观察，不得用于实盘排序或仓位；"
            "方案§6.1明确S6依赖P1校准结论支持后才启动回测，P1(#269)本机零样本UNVERIFIED，"
            "本轮注册条件比其他策略更严"
        ),
    }


def counterfactual(
    events: Sequence[Dict[str, Any]],
    *,
    sentiment_records: Optional[Sequence[Dict[str, Any]]] = None,
    leader_confirm: Optional[bool] = None,
    hold_mode: str = DEFAULT_HOLD_MODE,
    cost: Optional[Dict[str, float]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """反事实对照（方案 §6.2 第 1 条）：关掉 P5 成交约束后收益虚高多少。

    差值为正且样本非空，才说明约束真的在咬；否则回测里的约束是装饰。
    """
    on = run(events, sentiment_records=sentiment_records, leader_confirm=leader_confirm,
             hold_mode=hold_mode, constraints_enabled=True, cost=cost, cfg=cfg)
    off = run(events, sentiment_records=sentiment_records, leader_confirm=leader_confirm,
              hold_mode=hold_mode, constraints_enabled=False, cost=cost, cfg=cfg)
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


def load_sentiment_records(path: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    if not path:
        return None
    with open(os.path.abspath(os.path.expanduser(path)), encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return payload["records"]
    raise ValueError(f"情绪日报序列格式不认识：{path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="S6 冰点反转（IcePointReversal）回测 — NON-LIVE 研究用，不触网"
    )
    parser.add_argument("--table", required=True, help="已固化事件表 JSON 路径")
    parser.add_argument("--hold-mode", default=DEFAULT_HOLD_MODE, choices=list(HOLD_MODES))
    parser.add_argument("--sentiment-table", default=None,
                        help="情绪日报序列 JSON 路径（sentiment_daily 产出）；不给则 S_t/ΔS_t 一律 unavailable")
    parser.add_argument("--leader-confirm", choices=["true", "false"], default=None,
                        help="逆势活口是否被市场确认；不给则该条件 fail-closed")
    parser.add_argument("--counterfactual", action="store_true",
                        help="同时跑关闭成交约束的对照，报告收益虚高幅度")
    args = parser.parse_args()

    payload = load_events(args.table)
    sentiment_records = load_sentiment_records(args.sentiment_table)
    leader_confirm = {"true": True, "false": False, None: None}[args.leader_confirm]
    runner = counterfactual if args.counterfactual else run
    result = runner(
        payload["events"], sentiment_records=sentiment_records,
        leader_confirm=leader_confirm, hold_mode=args.hold_mode,
    )
    result["source_table"] = args.table
    result["source_schema"] = payload.get("schema")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
