#!/usr/bin/env python3
"""
S3 最强助攻套利（AssistArbitrage）回测接线 — 升级方案 §6.1 + §8.1(a)，NON-LIVE
=================================================================================
把 skills/common/assist_arbitrage.py 的纯信号接到既有打板回测事件表上：

  事件表(daban_bt_data v4) → 题材×交易日候选池记录 → 组内挑龙头(最高连板)→挂
  leader_score_shadow(复用 P2 hot_money_selection.leader_score，不重造)→
  assist_arbitrage.evaluate_universe → 命中集合 {(code, date)} →
  daban_bt_engine.strategy_returns（含 P5(a) 成交约束 + 既有费用口径 DEFAULT_COST
  + T+1 口径的 hold_mode）

**不新增网络请求**：只读已固化的事件表 JSON。

字段映射（事件表 → 信号记录）与诚实缺口标注
--------------------------------------------
board_height       = lianban                                   —— 连板高度
sector_breadth_count = sector_limitup_count                    —— 板块涨停家数
change_pct         = −封板分钟数(first_seal 越早值越大)          —— 相对强度排名依据
leader_confirmed   = 龙头(组内最高连板者) first_seal 不为空      —— 龙头当日已封板

change_pct 为什么不用当日涨跌幅：本 universe 全部是当日涨停事件，收盘价都钉在
（或极接近）当日涨停价，(t_close−t_prev_close)/t_prev_close 对每个样本几乎恒为
同一个百分比（10%涨停板的定义），完全没有区分度——拿它做"相对强度Top20%"排名会
把"谁先封板"这个真正有区分度的信号丢掉。改用封板早晚（同 LeaderScore 的
seal_speed 因子同构：越早封板=越强）作为代理，字段名沿用 change_pct 只是复用
assist_arbitrage.py 的默认 relative_strength_field 配置项，语义已在此处更正。

仍然缺的两个必需证据（诚实标注，不许拿近似值冒充）：
  - breakout_time：候选"率先突破日内关键位"需要盘中关键位检测，事件表（v3/v4）
    都没有这条分钟线派生管道，因此本适配器把它留空，入场触发条件在真实事件表上
    恒 unavailable。这不是"没有信号"，是数据缺口——同 S1 的 volume_ratio、S2 的
    pre_reseal_turnover_pct 一样，见 docs_private/ 的缺口清单。
  - leader_score_shadow：本脚本用 hot_money_selection.leader_score() 现算（复用
    P2 已合入的实现，不重造），但六因子里 seal_speed/resilience 仅深度池可得、
    relative_strength 需要全市场中位数与板块前十均值（回测事件表没有这两个基准）、
    attention 需要社交关注度快照——事件表能喂给它的通常只有 height(需要
    market_space_height，同样缺) 与 assist_breadth，可用权重大概率低于
    min_available_weight(0.60)，因此 LeaderScore 条件在真实事件表上大概率
    unavailable，命中为 0 或极少，这同样是数据缺口不是负结果。

对这些字段只做同名/近义透传（信号层判定一行未改）；带 sector 但不带其余字段的
记录，仍然诚实地判 unavailable，不拿间接推断顶替真实聚合。

红线：S3 未在 strategy_registry 注册。本脚本只产出研究数字，不得写回任何实盘状态。
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
import assist_arbitrage as aa  # noqa: E402
import execution_constraints as xc  # noqa: E402
import hot_money_selection as hms  # noqa: E402
from daban_bt_engine import (  # noqa: E402
    DEFAULT_COST,
    HOLD_MODES,
    filter_universe,
    parse_seal_minutes,
    strategy_returns,
)

SCHEMA = "assist_arbitrage_backtest_v1"
DEFAULT_HOLD_MODE = "board_overnight"


def event_record(event: Dict[str, Any]) -> Dict[str, Any]:
    """单个回测事件 → S3 候选记录（缺字段留 None，由信号层 fail-closed）。

    ``leader_score_shadow`` / ``leader_confirmed`` 不在这里算——它们依赖同组内的
    龙头识别，只有拿到整组记录之后才能算，见 ``_attach_leader_fields``。
    """
    seal_minutes = parse_seal_minutes(event.get("first_seal"))
    return {
        "code": str(event.get("code") or "").zfill(6),
        "date": str(event.get("date") or ""),
        "sector": event.get("sector"),
        "board_height": event.get("lianban"),
        "sector_breadth_count": event.get("sector_limitup_count"),
        # 越早封板值越大——见模块 docstring 对 change_pct 语义的说明。
        "change_pct": -float(seal_minutes) if seal_minutes is not None else None,
        # 事件表没有盘中关键位检测管道，诚实留空（见模块docstring的缺口标注）。
        "breakout_time": event.get("breakout_time"),
        "first_seal": event.get("first_seal"),
        "open_count": event.get("open_board_count"),
    }


def _attach_leader_fields(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 (date, sector) 分组，给组内每条记录标记是否为龙头 + 现算龙头的
    leader_score_shadow（复用 hot_money_selection.leader_score，不重造）。

    只**新增**字段：leader_confirmed（仅写在被选中的龙头记录上）与
    leader_score_shadow（同上）。候选自身记录不变。
    """
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for record in records:
        key = (record.get("date"), str(record.get("sector") or ""))
        groups.setdefault(key, []).append(record)

    out: List[Dict[str, Any]] = []
    for key, group in groups.items():
        leader = aa.pick_leader(group)
        leader_code = str(leader.get("code")) if leader is not None else None
        sector_state = {"limitup_count": group[0].get("sector_breadth_count")}
        for record in group:
            row = dict(record)
            if leader_code and row.get("code") == leader_code:
                row["leader_confirmed"] = row.get("first_seal") is not None
                row["leader_score_shadow"] = hms.leader_score(
                    row, sector_state=sector_state, in_deep_pool=False,
                )
            out.append(row)
    return out


def event_records(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    mapped = [event_record(event) for event in filter_universe(list(events))]
    return _attach_leader_fields(mapped)


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
    """跑一轮 S3 回测。constraints_enabled=False 只用于反事实对照，绝非可执行口径。"""
    if hold_mode not in HOLD_MODES:
        raise ValueError(f"unknown hold_mode: {hold_mode}; allowed {HOLD_MODES}")
    all_events = list(events)
    records = event_records(all_events)
    results = aa.evaluate_universe(records, cfg=cfg)
    fired = aa.signal_codes(results)

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
        # records 含被选中的龙头行（它不作为候选被评估，见 evaluate_group）；
        # 诚实口径是"实际被判定过的候选数"= len(results)，不是原始记录数。
        "universe_count": len(results),
        "signal_summary": aa.summarize(results),
        "signal_count": len(fired),
        "filled_count": len(returns),
        "returns": _stats(returns),
        "degraded": sorted({d for r in results for d in r.get("degraded") or []}),
        "registered_in_strategy_registry": False,
        "note": "S3 未过 research_gate/未注册，本结果只作研究观察，不得用于实盘排序或仓位",
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
        description="S3 最强助攻套利（AssistArbitrage）回测 — NON-LIVE 研究用，不触网"
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
