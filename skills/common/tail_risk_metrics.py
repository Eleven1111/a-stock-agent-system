"""尾部风险与分阶段绩效指标（升级方案 P5-d）。

研究报告的核心主张之一：对情绪短线策略，**GapRisk 与 LimitDownRisk 的重要性甚至
高于 Sharpe**。原因是涨停接力的风险是非线性的——买入后炸板、当日不能反向卖出、
次日可能低开、极端情况下跌停附近没有流动性，于是预设的 5% 止损会变成 10%、15%。
只看均值和夏普会系统性低估这条左尾。

因此本模块只做一件事：把"平均赚多少"之外的东西算出来——

- ``gap_risk``          次日开盘跌破阈值的条件概率 P(open_gap <= -x% | signal)
- ``limit_down_risk``   次日跌停的条件概率
- ``mae`` / ``mfe``     最大不利/有利波动（持仓期内的极值，不是收盘值）
- ``avg_r``             以初始风险为单位的平均收益，使不同波动的标的可比
- ``expectancy``        pW − (1−p)L
- ``state_pnl``         按情绪阶段拆分的收益矩阵（报告称之为核心指标）

统一的空集纪律（与 sentiment_score / state_pnl_report 一致）：**样本为空一律返回
``unavailable`` 而不是 0.0**。"这个信号没有跌停风险"和"这个信号没有样本"必须可区分——
把后者显示成 0.0 是本仓黑名单里"空集恒真"那一类假绿，而且方向恰好是危险的那一侧
（把未知的尾部风险显示成零风险）。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

AVAILABLE = "available"
UNAVAILABLE = "unavailable"

__all__ = [
    "AVAILABLE",
    "UNAVAILABLE",
    "gap_risk",
    "limit_down_risk",
    "mae",
    "mfe",
    "avg_r",
    "expectancy",
    "state_pnl",
]


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": UNAVAILABLE, "value": None, "n": 0, "reason": reason, **extra}


def _clean(values: Iterable[Any]) -> list[float]:
    out: list[float] = []
    for value in values or ():
        if value is None:
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def _rate(hits: int, total: int, *, reason: str) -> dict[str, Any]:
    if total <= 0:
        return _unavailable(reason)
    return {
        "status": AVAILABLE,
        "value": round(hits / total, 6),
        "n": total,
        "hits": hits,
        "reason": None,
    }


def gap_risk(next_open_gaps_pct: Sequence[Any], *, threshold_pct: float = -5.0
             ) -> dict[str, Any]:
    """P(次日开盘跳空 <= threshold | signal)。

    入参是**次日开盘相对今收的百分比**，负数表示低开。阈值取负值（默认 −5%）。
    """
    sample = _clean(next_open_gaps_pct)
    hits = sum(1 for gap in sample if gap <= float(threshold_pct))
    result = _rate(hits, len(sample), reason="no_gap_sample")
    result["threshold_pct"] = float(threshold_pct)
    return result


def limit_down_risk(next_day_limit_down_flags: Sequence[Any]) -> dict[str, Any]:
    """P(次日跌停 | signal)。入参是逐笔的布尔标记（None 视为缺失，不计入分母）。"""
    sample = [bool(flag) for flag in (next_day_limit_down_flags or ()) if flag is not None]
    return _rate(sum(1 for flag in sample if flag), len(sample),
                 reason="no_limit_down_sample")


def _extreme(paths: Sequence[Sequence[Any]], *, adverse: bool) -> dict[str, Any]:
    """逐笔取持仓路径的极值再平均。

    必须用**路径极值**而不是收盘收益：一笔最终收平、但盘中一度 −12% 的交易，
    在收盘口径下完全看不见，而它正是 T+1 下最可能被扫掉的那种。
    """
    per_trade: list[float] = []
    for path in paths or ():
        points = _clean(path)
        if not points:
            continue
        per_trade.append(min(points) if adverse else max(points))
    if not per_trade:
        return _unavailable("no_path_sample")
    return {
        "status": AVAILABLE,
        "value": round(sum(per_trade) / len(per_trade), 6),
        "n": len(per_trade),
        "worst": round(min(per_trade), 6) if adverse else round(max(per_trade), 6),
        "reason": None,
    }


def mae(paths: Sequence[Sequence[Any]]) -> dict[str, Any]:
    """平均最大不利波动（Maximum Adverse Excursion）。"""
    return _extreme(paths, adverse=True)


def mfe(paths: Sequence[Sequence[Any]]) -> dict[str, Any]:
    """平均最大有利波动（Maximum Favourable Excursion）。"""
    return _extreme(paths, adverse=False)


def avg_r(returns_pct: Sequence[Any], initial_risk_pct: Sequence[Any]) -> dict[str, Any]:
    """以初始风险为单位的平均收益。

    R 化的意义是让不同波动率的标的可比：赚 3% 在 1% 风险下是 3R，在 6% 风险下只有
    0.5R。初始风险 <= 0 的记录直接丢弃并计数——除以零得到的 inf 会污染整个均值。
    """
    pairs = list(zip(_clean(returns_pct), _clean(initial_risk_pct)))
    if len(_clean(returns_pct)) != len(_clean(initial_risk_pct)):
        return _unavailable("length_mismatch")
    usable = [(ret, risk) for ret, risk in pairs if risk > 0]
    dropped = len(pairs) - len(usable)
    if not usable:
        return _unavailable("no_positive_risk_sample", dropped=dropped)
    values = [ret / risk for ret, risk in usable]
    return {
        "status": AVAILABLE,
        "value": round(sum(values) / len(values), 6),
        "n": len(values),
        "dropped_non_positive_risk": dropped,
        "reason": None,
    }


def expectancy(returns_pct: Sequence[Any]) -> dict[str, Any]:
    """期望值 pW − (1−p)L。平局（0）计入分母但不计入盈亏两侧。"""
    sample = _clean(returns_pct)
    if not sample:
        return _unavailable("no_return_sample")
    wins = [value for value in sample if value > 0]
    losses = [value for value in sample if value < 0]
    win_rate = len(wins) / len(sample)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    return {
        "status": AVAILABLE,
        "value": round(win_rate * avg_win - (1.0 - win_rate) * avg_loss, 6),
        "n": len(sample),
        "win_rate": round(win_rate, 6),
        "avg_win": round(avg_win, 6),
        "avg_loss": round(avg_loss, 6),
        "reason": None,
    }


def state_pnl(records: Sequence[Mapping[str, Any]], *, min_samples: int = 30
              ) -> dict[str, Any]:
    """按情绪阶段拆分收益。报告称之为核心指标：真有状态依赖才谈得上"情绪周期"。

    与 ``scripts/state_pnl_report.py`` 同一套纪律：**低于样本门槛的格子标
    UNVERIFIED 并扣住均值**——不足 30 笔算出来的均值拿去做阶段对比，比不算更糟。
    """
    buckets: dict[str, list[float]] = {}
    for row in records or ():
        if not isinstance(row, Mapping):
            continue
        state = row.get("state")
        value = row.get("return_pct")
        if state is None or value is None:
            continue
        try:
            buckets.setdefault(str(state), []).append(float(value))
        except (TypeError, ValueError):
            continue
    if not buckets:
        return {"status": UNAVAILABLE, "cells": {}, "reason": "no_state_sample"}
    cells: dict[str, dict[str, Any]] = {}
    for state, values in sorted(buckets.items()):
        if len(values) < int(min_samples):
            cells[state] = {"n": len(values), "status": "UNVERIFIED", "mean": None,
                            "withheld_reason": f"n<{int(min_samples)}"}
        else:
            cells[state] = {"n": len(values), "status": AVAILABLE,
                            "mean": round(sum(values) / len(values), 6)}
    return {
        "status": AVAILABLE if any(c["status"] == AVAILABLE for c in cells.values())
        else UNAVAILABLE,
        "cells": cells,
        "min_samples": int(min_samples),
        "reason": None,
    }
