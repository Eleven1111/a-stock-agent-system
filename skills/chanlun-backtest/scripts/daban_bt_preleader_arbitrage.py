#!/usr/bin/env python3
"""
S4 先于龙头套利（PreleaderArbitrage）回测接线 — 升级方案 §6.1 + §8.1(a)，NON-LIVE
=====================================================================================
把 skills/common/preleader_arbitrage.py 的纯信号接到既有打板回测事件表上：

  事件表(daban_bt_data v4) → 按 sector 当"属性" → 逐交易日滚动：用**前一个出现
  在事件表里的交易日**的记录构建盘前表(build_pretable，只用那一天的数据) →
  当日候选池(挂上当日确认的龙头) → preleader_arbitrage.evaluate_universe →
  命中集合 {(code, date)} → daban_bt_engine.strategy_returns（含 P5(a) 成交约束
  + 既有费用口径 DEFAULT_COST + T+1 口径的 hold_mode）

**不新增网络请求**：只读已固化的事件表 JSON。

字段映射（事件表 → 信号记录）与诚实缺口标注
--------------------------------------------
attribute        = sector                                    —— 属性归类（复用既有 sector 字段，不自造第三套题材体系）
evaluation_time  = first_seal（候选自身当日封板时刻）           —— 候选自身"反应"的时刻
amount           = t_amount（候选当日成交额）                   —— 条件4流动性依据
board_height     = lianban（连板高度，只用于挑当日龙头候选）

confirmed / confirmed_time 只标在"当日龙头"记录上（组内 lianban 最高且
first_seal 不为空者，同 assist_arbitrage.pick_leader 同构的挑法，只是判据换成
当日连板高度而不是历史龙头分）。

盘前表滚动构建口径（诚实标注，这是本适配器的核心约束）
--------------------------------------------------
真实生产环境的盘前表来自 D-1 晚间对全市场的题材/龙头分析，本仓库尚无这条独立
管道。回测层只能退而求其次：用事件表里"前一个出现的交易日"的记录当作 D-1
证据，构建当天要用的盘前表——``build_pretable`` 本身依旧只吃传入的 as_of 及更
早数据，这一点与 skills/common/preleader_arbitrage.py 的纪律完全一致，本适配器
额外新增的假设只是"用事件表能提供的最近一个交易日代替真正的全市场 D-1 扫描"。
事件表的第一个交易日没有更早的日期可用 → 该日盘前表为空表，全部候选当天必然
no_signal（不在任何(龙头,属性)条目内），这是诚实的边界情况，不是 bug。

仍然缺的证据（诚实标注，不许拿近似值冒充）：
  - material_bad_news：事件表没有个股利空事件流，盘前表构建时对这一项恒当
    False（即"建表阶段不做利空剔除"），与 S3 的诚实缺口标注同构——这不是
    "利空不存在"，是数据缺口，见 docs/preleader-arbitrage-gate-evaluation-2026-08.md；
  - avg_turnover_20d：本适配器用候选当日成交额 t_amount 作代理（不是真正的
    20 日均值），与 change_pct 用封板早晚代理相对强度同类处理，语义已在此更正。

红线：S4 未在 strategy_registry 注册。本脚本只产出研究数字，不得写回任何实盘状态。
"""

from __future__ import annotations

import argparse
import json
import os
from statistics import mean, median
from typing import Any, Dict, List, Optional, Sequence

import skills.common  # noqa: F401  -- puts skills/common on sys.path

import preleader_arbitrage as pa  # noqa: E402
import execution_constraints as xc  # noqa: E402
from daban_bt_engine import (  # noqa: E402
    DEFAULT_COST,
    HOLD_MODES,
    filter_universe,
    strategy_returns,
)

SCHEMA = "preleader_arbitrage_backtest_v1"
DEFAULT_HOLD_MODE = "board_overnight"


def event_record(event: Dict[str, Any]) -> Dict[str, Any]:
    """单个回测事件 → S4 候选记录（缺字段留 None，由信号层 fail-closed）。

    ``confirmed`` / ``confirmed_time`` 不在这里算——它们依赖同组(同日同属性)内
    的龙头识别，只有拿到整组记录之后才能算，见 ``_pick_daily_leader`` /
    ``_attach_confirmation_fields``。
    """
    return {
        "code": str(event.get("code") or "").zfill(6),
        "date": str(event.get("date") or ""),
        "attribute": event.get("sector"),
        "evaluation_time": event.get("first_seal"),
        "amount": event.get("t_amount"),
        "board_height": event.get("lianban"),
        "first_seal": event.get("first_seal"),
        "is_st": event.get("is_st"),
    }


def _pick_daily_leader(group: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """组内挑当日龙头候选：连板高度最高且已封板(first_seal 非空)者；并列按
    code 升序取最小。没有任何一个 peer 带可用 board_height 时返回 None。"""
    candidates = [
        (record, record.get("board_height"))
        for record in group if record.get("first_seal") is not None
    ]
    candidates = [(r, h) for r, h in candidates if isinstance(h, (int, float))]
    if not candidates:
        return None
    best_height = max(h for _r, h in candidates)
    tied = [r for r, h in candidates if h == best_height]
    tied.sort(key=lambda r: str(r.get("code")))
    return dict(tied[0])


def _attach_confirmation_fields(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 (date, attribute) 分组，给组内被选中的当日龙头标记 confirmed /
    confirmed_time。只新增字段，候选自身记录不变。"""
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for record in records:
        key = (record.get("date"), str(record.get("attribute") or ""))
        groups.setdefault(key, []).append(record)

    out: List[Dict[str, Any]] = []
    for group in groups.values():
        leader = _pick_daily_leader(group)
        leader_code = str(leader.get("code")) if leader is not None else None
        for record in group:
            row = dict(record)
            if leader_code and row.get("code") == leader_code:
                row["confirmed"] = row.get("first_seal") is not None
                row["confirmed_time"] = row.get("first_seal")
            out.append(row)
    return out


def _build_pretables_by_date(records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """按事件表里出现的交易日顺序滚动构建盘前表：每天用的表来自"前一个出现
    在事件表里的交易日"的记录（诚实标注见模块 docstring）。第一天没有更早的
    日期可用 → 空表。"""
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        by_date.setdefault(str(record.get("date") or ""), []).append(record)
    dates = sorted(d for d in by_date if d)

    tables: Dict[str, Dict[str, Any]] = {}
    prev_date: Optional[str] = None
    for current in dates:
        if prev_date is None:
            tables[current] = pa.build_pretable([], [], as_of=current)
        else:
            prev_group = _attach_confirmation_fields(by_date[prev_date])
            leader = _pick_daily_leader(prev_group)
            leader_records: List[Dict[str, Any]] = []
            if leader is not None and str(leader.get("attribute") or ""):
                leader_records = [{
                    "code": leader.get("code"),
                    "attribute": leader.get("attribute"),
                    "date": prev_date,
                }]
            member_records = [
                {
                    "code": record.get("code"),
                    "attribute": record.get("attribute"),
                    "date": prev_date,
                    "is_st": record.get("is_st"),
                    "material_bad_news": False,  # 诚实缺口：事件表无利空事件流，见模块 docstring
                    "avg_turnover_20d": record.get("amount"),  # 代理值，非真实20日均值
                }
                for record in prev_group
            ]
            tables[current] = pa.build_pretable(
                leader_records, member_records, as_of=prev_date,
            )
        prev_date = current
    return tables


def event_records(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    mapped = [event_record(event) for event in filter_universe(list(events))]
    return _attach_confirmation_fields(mapped)


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
    """跑一轮 S4 回测。constraints_enabled=False 只用于反事实对照，绝非可执行口径。"""
    if hold_mode not in HOLD_MODES:
        raise ValueError(f"unknown hold_mode: {hold_mode}; allowed {HOLD_MODES}")
    all_events = list(events)
    records = event_records(all_events)
    pretables = _build_pretables_by_date(records)

    results: List[Dict[str, Any]] = []
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for record in records:
        key = (str(record.get("date") or ""), str(record.get("attribute") or ""))
        groups.setdefault(key, []).append(record)
    for key in sorted(groups):
        date = key[0]
        results.extend(pa.evaluate_group(
            groups[key], pretable=pretables.get(date), cfg=cfg,
        ))
    fired = pa.signal_codes(results)

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
        # records 含被选中的当日龙头行（它不作为候选被评估，见 evaluate_group）；
        # 诚实口径是"实际被判定过的候选数"= len(results)，不是原始记录数。
        "universe_count": len(results),
        "signal_summary": pa.summarize(results),
        "signal_count": len(fired),
        "filled_count": len(returns),
        "returns": _stats(returns),
        "degraded": sorted({d for r in results for d in r.get("degraded") or []}),
        "registered_in_strategy_registry": False,
        "note": "S4 未过 research_gate/未注册，本结果只作研究观察，不得用于实盘排序或仓位",
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
        description="S4 先于龙头套利（PreleaderArbitrage）回测 — NON-LIVE 研究用，不触网"
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
