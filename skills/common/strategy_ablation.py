"""消融实验：逐级验证每个模块是否真有统计增量（升级方案 P5-c）。

研究报告的要求很直白：要证明赚钱的是"情绪系统"而不是偶然的涨停暴露，就得把系统
一层层拆开比：

    A 只买涨停 → B +板块≥3 → C +情绪过滤 → D +龙头评分 → E +超预期
    → F +1+1+1 仓位 → G +退潮禁入

然后看 ΔCAGR / ΔMaxDD / ΔPF / ΔTailLoss。**如果从 D→E→F→G 没有稳定改善，相应模块
就没有统计价值** —— 报告的原话。这句话的执行含义是：消融不过的模块应当删掉，而不是
留着当装饰。本模块负责把这件事变成可跑的对照，不负责替人做删留决定。

三条纪律，每条都对应一种会让消融结论作废的假绿：

1. **逐级嵌套是结构性保证**。每一级都在上一级的 surviving 上再过滤，样本只可能收缩，
   因此各级之间天然可比。这里刻意**不**加"若不是子集就报错"的运行时守卫——那条分支
   永远走不到，是死的防御代码，只会给出虚假保障（测试改为断言样本单调收缩）。
2. **样本数必须随结果一起呈现，且低于门槛不给结论**。逐级过滤天然会把样本削薄，
   到 G 级往往只剩个位数——此时 ΔPF 的正负号毫无意义。低于门槛的级别标 UNVERIFIED
   并扣住 delta，与 state_pnl / tail_risk_metrics 同一套口径。
3. **空集不出恒真数**。某一级零样本时 profit_factor 不是 0.0 也不是 inf，是
   ``unavailable``。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
UNVERIFIED = "UNVERIFIED"

#: 报告 §"消融实验"给出的七级阶梯。名字即该级新增的那一个条件。
LADDER = ("A_limit_up_only", "B_sector_breadth", "C_sentiment_filter",
          "D_leader_score", "E_surprise", "F_position_1_1_1", "G_retreat_block")

__all__ = ["AVAILABLE", "UNAVAILABLE", "UNVERIFIED", "LADDER",
           "profit_factor", "max_drawdown", "tail_loss", "level_metrics",
           "run_ablation"]


def _clean(values: Sequence[Any]) -> list[float]:
    out: list[float] = []
    for value in values or ():
        if value is None:
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def profit_factor(returns: Sequence[Any]) -> dict[str, Any]:
    """总盈利 / 总亏损。

    全胜（没有任何亏损）时不返回 ``inf`` —— 那个数字会在下游的 delta 计算里污染
    一整列。返回 ``unavailable`` 并说明原因，让读的人知道是"无亏损样本"而不是
    "盈亏比无穷大"。
    """
    sample = _clean(returns)
    if not sample:
        return {"status": UNAVAILABLE, "value": None, "reason": "empty_sample"}
    gains = sum(value for value in sample if value > 0)
    losses = -sum(value for value in sample if value < 0)
    if losses <= 0:
        return {"status": UNAVAILABLE, "value": None, "reason": "no_losing_trades"}
    return {"status": AVAILABLE, "value": round(gains / losses, 6), "reason": None}


def max_drawdown(returns: Sequence[Any]) -> dict[str, Any]:
    """按顺序复利后的最大回撤（负数）。样本为空 → unavailable。"""
    sample = _clean(returns)
    if not sample:
        return {"status": UNAVAILABLE, "value": None, "reason": "empty_sample"}
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in sample:
        equity *= (1.0 + value)
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return {"status": AVAILABLE, "value": round(worst, 6), "reason": None}


def tail_loss(returns: Sequence[Any], *, quantile: float = 0.05) -> dict[str, Any]:
    """左尾均值（最差 quantile 部分的平均收益）。

    情绪短线策略的差异往往不在均值而在左尾，所以消融必须单独报它。
    """
    sample = sorted(_clean(returns))
    if not sample:
        return {"status": UNAVAILABLE, "value": None, "reason": "empty_sample"}
    count = max(1, int(len(sample) * float(quantile)))
    worst = sample[:count]
    return {"status": AVAILABLE, "value": round(sum(worst) / len(worst), 6),
            "n_tail": count, "reason": None}


def level_metrics(returns: Sequence[Any], *, min_samples: int) -> dict[str, Any]:
    """单级指标。低于样本门槛 → UNVERIFIED 且**扣住数值**。"""
    sample = _clean(returns)
    base = {"n": len(sample)}
    if not sample:
        return {**base, "status": UNAVAILABLE, "mean": None,
                "profit_factor": profit_factor(sample),
                "max_drawdown": max_drawdown(sample),
                "tail_loss": tail_loss(sample)}
    if len(sample) < int(min_samples):
        return {**base, "status": UNVERIFIED, "mean": None,
                "withheld_reason": f"n<{int(min_samples)}",
                "profit_factor": {"status": UNVERIFIED, "value": None},
                "max_drawdown": {"status": UNVERIFIED, "value": None},
                "tail_loss": {"status": UNVERIFIED, "value": None}}
    return {**base, "status": AVAILABLE,
            "mean": round(sum(sample) / len(sample), 6),
            "profit_factor": profit_factor(sample),
            "max_drawdown": max_drawdown(sample),
            "tail_loss": tail_loss(sample)}


def _delta(current: Mapping[str, Any], previous: Mapping[str, Any], key: str
           ) -> dict[str, Any]:
    """两级之间某指标的差。任一侧不可用 → 差值也不可用，不拿 0 顶替。"""
    left = current.get(key) or {}
    right = previous.get(key) or {}
    if left.get("status") != AVAILABLE or right.get("status") != AVAILABLE:
        return {"status": UNAVAILABLE, "value": None,
                "reason": "one_side_unavailable"}
    return {"status": AVAILABLE,
            "value": round(float(left["value"]) - float(right["value"]), 6),
            "reason": None}


def run_ablation(events: Sequence[Mapping[str, Any]],
                 predicates: Mapping[str, Callable[[Mapping[str, Any]], bool]],
                 return_of: Callable[[Mapping[str, Any]], Any],
                 *, ladder: Sequence[str] = LADDER,
                 min_samples: int = 30) -> dict[str, Any]:
    """跑一遍 A→G 阶梯。

    ``predicates[level]`` 是该级**新增**的那一个条件；每一级在前一级已命中的事件
    上再过滤，因此样本天然逐级收缩。缺某一级的谓词就在结果里标出来并跳过，不静默
    当作"该级无条件"——那会让阶梯看起来完整、实际少了一环。
    """
    surviving = [dict(row) for row in (events or []) if isinstance(row, Mapping)]
    levels: dict[str, Any] = {}
    previous_metrics: dict[str, Any] | None = None
    missing: list[str] = []

    for level in ladder:
        predicate = predicates.get(level)
        if predicate is None:
            missing.append(level)
            levels[level] = {"status": UNAVAILABLE, "n": 0,
                             "reason": "predicate_not_supplied"}
            continue
        # 嵌套是结构性保证而非运行时检查：每级都在上一级的 surviving 上再过滤，
        # 样本只可能收缩。这里刻意不写"若不是子集就报错"的守卫——那条分支永远
        # 走不到，是死的防御代码，只会给出虚假保障。
        surviving = [row for row in surviving if predicate(row)]
        metrics = level_metrics([return_of(row) for row in surviving],
                                min_samples=min_samples)
        if previous_metrics is not None:
            metrics["delta_vs_previous"] = {
                "mean": _delta({"mean_box": {"status": metrics["status"],
                                             "value": metrics.get("mean")}},
                               {"mean_box": {"status": previous_metrics["status"],
                                             "value": previous_metrics.get("mean")}},
                               "mean_box"),
                "profit_factor": _delta(metrics, previous_metrics, "profit_factor"),
                "max_drawdown": _delta(metrics, previous_metrics, "max_drawdown"),
                "tail_loss": _delta(metrics, previous_metrics, "tail_loss"),
            }
        levels[level] = metrics
        previous_metrics = metrics

    return {
        "schema": "strategy_ablation_v1",
        "calibrated": False,
        "min_samples": int(min_samples),
        "levels": levels,
        "missing_predicates": missing,
        "note": ("逐级样本天然收缩；低于门槛的级别已扣住数值。"
                 "某级无稳定改善即说明该模块没有统计价值，应当删除而不是保留装饰。"),
    }
