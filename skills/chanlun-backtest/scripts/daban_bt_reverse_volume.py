#!/usr/bin/env python3
"""
S5 反量龙回头（ReverseVolume）回测接线 — 升级方案 §6.1 + §8.1(a)，NON-LIVE
=================================================================================
把 skills/common/reverse_volume.py 的纯信号接到既有打板回测事件表上：

  事件表(daban_bt_data v4) → 单事件记录（无需题材分组——回撤/反量都是标的自身
  历史，不是横截面比较）→ reverse_volume.evaluate_universe → 命中集合
  {(code, date)} → daban_bt_engine.strategy_returns（含 P5(a) 成交约束 + 既有
  费用口径 DEFAULT_COST + T+1 口径的 hold_mode）

**不新增网络请求**：只读已固化的事件表 JSON。

字段映射（事件表 → 信号记录）与诚实缺口标注
--------------------------------------------
既有 daban_bt_data(v3/v4) 事件表是"单日涨停快照"结构（每条记录=一次T日涨停 +
T+1表现），本策略需要的七类证据全部是**跨周期/跨日的时间序列证据**，事件表结构
上就不携带：

  - was_prior_period_top_leader：需要跨周期的人气/连板高度历史排名，事件表逐日
    独立快照，没有"上一周期"的概念。
  - drawdown_pct：需要标的自己的历史最高价与当前价格，事件表只有 T/T1 两日
    OHLC，没有更早的高点序列。
  - market_sentiment（大盘/情绪是否不再加速恶化）：需要外部市场状态口径
    （同 S1 的 market_state），本脚本开放 --market-state 参数透传，不给则
    fail-closed（同 rank_surprise 的 theme_alive 处理方式）。
  - volatility_contraction_ratio / volume_percentile_20d：需要标的至少
    20+ 个交易日的日线序列，事件表没有落这条数据。
  - max_up_minute_volume / max_down_minute_volume_prior /
    pullback_max_down_minute_volume / second_max_up_minute_volume：这三个
    是本策略的核心证据，必须来自逐分钟行情（复用 skills/common/
    minute_derived.py + reverse_volume.max_directional_minute_volume）。
    事件表 v4 只固化了 09:45 量比这一个**标量**派生值（`volume_ratio`），
    不落原始分钟行——同 S1 的 volume_ratio 缺口、S2 的 pre_reseal_turnover_pct
    缺口一样，是分钟线派生管道尚未覆盖到"多个时间窗口的方向性峰值"这一层
    （见 docs/minute-derived-pipeline-2026-08.md）。要跑出非零样本，必须先把
    "入场前/回踩期"两个时间窗口的分钟行落盘或改造 minute_rows_source 支持
    多窗口检索。
  - breakout_above_balance_zone：需要盘中"短期平衡区"检测（同 S3 的
    breakout_time 缺口同构），事件表没有这条盘中关键位管道。

因此本适配器对以上字段一律留 ``None``，交给 reverse_volume.evaluate() 按
fail-closed 规则判 unavailable——**不造代理值**，零命中是数据缺口不是"策略没有
信号"。

红线：S5 未在 strategy_registry 注册。本脚本只产出研究数字，不得写回任何实盘状态。
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
import reverse_volume as rv  # noqa: E402
from daban_bt_engine import (  # noqa: E402
    DEFAULT_COST,
    HOLD_MODES,
    filter_universe,
    strategy_returns,
)

SCHEMA = "reverse_volume_backtest_v1"
DEFAULT_HOLD_MODE = "board_overnight"

# 事件表结构性不携带的证据字段——诚实留空，绝不拿近似值/代理值顶替（见模块docstring）。
_MISSING_EVIDENCE_FIELDS = (
    "was_prior_period_top_leader",
    "drawdown_pct",
    "volatility_contraction_ratio",
    "volume_percentile_20d",
    "max_up_minute_volume",
    "max_down_minute_volume_prior",
    "pullback_max_down_minute_volume",
    "second_max_up_minute_volume",
    "breakout_above_balance_zone",
)


def event_record(event: Dict[str, Any]) -> Dict[str, Any]:
    """单个回测事件 → S5 信号记录。

    事件表 v3/v4 是单日涨停快照结构，本策略需要的全部七类证据都是跨周期/
    跨分钟的时间序列证据，事件表结构上不携带——一律留 None 交信号层
    fail-closed 成 unavailable（见模块 docstring 的缺口清单）。
    """
    record: Dict[str, Any] = {
        "code": str(event.get("code") or "").zfill(6),
        "date": str(event.get("date") or ""),
    }
    for field in _MISSING_EVIDENCE_FIELDS:
        record[field] = None
    return record


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
    """跑一轮 S5 回测。constraints_enabled=False 只用于反事实对照，绝非可执行口径。"""
    if hold_mode not in HOLD_MODES:
        raise ValueError(f"unknown hold_mode: {hold_mode}; allowed {HOLD_MODES}")
    all_events = list(events)
    records = event_records(all_events)
    results = rv.evaluate_universe(records, market_state=market_state, cfg=cfg)
    fired = rv.signal_codes(results)

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
        "signal_summary": rv.summarize(results),
        "signal_count": len(fired),
        "filled_count": len(returns),
        "returns": _stats(returns),
        "degraded": sorted({d for r in results for d in r.get("degraded") or []}),
        "registered_in_strategy_registry": False,
        "note": "S5 未过 research_gate/未注册，本结果只作研究观察，不得用于实盘排序或仓位",
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
        description="S5 反量龙回头（ReverseVolume）回测 — NON-LIVE 研究用，不触网"
    )
    parser.add_argument("--table", required=True, help="已固化事件表 JSON 路径")
    parser.add_argument("--hold-mode", default=DEFAULT_HOLD_MODE, choices=list(HOLD_MODES))
    parser.add_argument("--market-state", default=None,
                        help="大盘/情绪是否恶化的外部状态标记（deteriorating/stable）；不给则该条件 fail-closed")
    parser.add_argument("--counterfactual", action="store_true",
                        help="同时跑关闭成交约束的对照，报告收益虚高幅度")
    args = parser.parse_args()

    payload = load_events(args.table)
    state = (
        {"available": True, "deteriorating": args.market_state == "deteriorating"}
        if args.market_state else None
    )
    runner = counterfactual if args.counterfactual else run
    result = runner(payload["events"], market_state=state, hold_mode=args.hold_mode)
    result["source_table"] = args.table
    result["source_schema"] = payload.get("schema")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
