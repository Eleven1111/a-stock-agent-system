"""横截面方向检验：高分是否真的比低分好。

现有研究闸门（``chan_signal_backtest`` + ``research_gate``）做的是**事件级**
判定——某个信号发出后 T+1/T+3 的净收益是否胜过对照。它回答不了另一个问题：
**一个横截面打分的排序方向对不对**。

``trend_score`` 正是从这个盲区漏过去的：它从未被要求证明高分优于低分。
2026-08-08 用部署机 lifecycle 数据实测，其中窗口 rank IC 为 -0.34、8/8 队列
全负、十分位近似单调倒挂（见 docs/trend-score-ic-evaluation-2026-08.md）。

三条设计取舍，都来自那次分析踩到的坑：

1. **独立性必须显式计算。** 15 个窗口看着很多，但前瞻窗口互相重叠、多数共享
   同一终点时，有效独立观测只有 2~3 个。本模块用贪心法数**互不重叠**的窗口，
   并以此作为判定门槛，而不是用窗口总数。
2. **方向反了要单独成一类**，不能和「没信号」混为一谈——两者的处置完全不同。
3. **样本不足一律 insufficient**，绝不因为「碰巧全正」判通过；常数分数（秩相关
   无定义）直接跳过而不是算出 0。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "cross_sectional_direction_v1"

# 判定参数固定为常量：调参 = 重新过闸，与 pullback_strategy.PARAMS 同一纪律。
PARAMS: dict[str, float | int] = {
    "min_pairs_per_cohort": 100,   # 单个队列的最小配对数
    "min_independent_cohorts": 5,  # 互不重叠窗口的最小个数
    "consistency_ratio": 0.70,     # 同号队列占比门槛
    "min_abs_mean_ic": 0.02,       # 低于此视为无方向（噪音带）
    "decile_count": 10,
}


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        average = (index + end) / 2.0
        for position in order[index:end + 1]:
            ranks[position] = average
        index = end + 1
    return ranks


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    sx = sum(value * value for value in dx) ** 0.5
    sy = sum(value * value for value in dy) ** 0.5
    if sx == 0 or sy == 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / (sx * sy)


def rank_ic(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Spearman 秩相关；分数或收益为常数时返回 None（无定义，不是 0）。"""
    if len(pairs) < 2:
        return None
    scores = [float(s) for s, _ in pairs]
    returns = [float(r) for _, r in pairs]
    return _pearson(_ranks(scores), _ranks(returns))


def decile_spread(pairs: Sequence[tuple[float, float]]) -> float | None:
    """最高分十分位与最低分十分位的平均收益差。"""
    buckets = int(PARAMS["decile_count"])
    if len(pairs) < buckets:
        return None
    ordered = sorted(pairs, key=lambda item: -item[0])
    size = len(ordered) // buckets
    if size == 0:
        return None
    top = [r for _, r in ordered[:size]]
    bottom = [r for _, r in ordered[-size:]]
    return sum(top) / len(top) - sum(bottom) / len(bottom)


def count_independent(windows: Iterable[tuple[str, str]]) -> int:
    """互不重叠的窗口个数（贪心按结束日排序）。

    前瞻窗口重叠的队列共享同一段行情，不构成独立观测。这一条是本模块存在的
    主要理由：不显式算它，就会把 15 个高度重叠的窗口当成 15 个证据。
    """
    ordered = sorted({(str(a), str(b)) for a, b in windows}, key=lambda w: (w[1], w[0]))
    count, last_end = 0, ""
    for start, end in ordered:
        if start >= last_end:
            count += 1
            last_end = end
    return count


def evaluate(cohorts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """对若干队列做横截面方向判定。

    每个 cohort: ``{"src": 信号日, "dst": 前瞻日, "pairs": [(score, forward_return)]}``
    """
    usable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for cohort in cohorts:
        pairs = [
            (float(s), float(r))
            for s, r in (cohort.get("pairs") or [])
        ]
        src, dst = str(cohort.get("src") or ""), str(cohort.get("dst") or "")
        if len(pairs) < int(PARAMS["min_pairs_per_cohort"]):
            skipped.append({"src": src, "dst": dst, "reason": "pairs_below_minimum", "n": len(pairs)})
            continue
        ic = rank_ic(pairs)
        if ic is None:
            skipped.append({"src": src, "dst": dst, "reason": "rank_ic_undefined", "n": len(pairs)})
            continue
        usable.append({
            "src": src, "dst": dst, "n": len(pairs),
            "ic": round(ic, 4), "decile_spread": decile_spread(pairs),
        })

    independent = count_independent((item["src"], item["dst"]) for item in usable)
    ics = [item["ic"] for item in usable]
    mean_ic = sum(ics) / len(ics) if ics else None
    positive_ratio = (
        sum(1 for value in ics if value > 0) / len(ics) if ics else None
    )
    spreads = [item["decile_spread"] for item in usable if item["decile_spread"] is not None]
    mean_spread = sum(spreads) / len(spreads) if spreads else None

    verdict = _verdict(usable, independent, mean_ic, positive_ratio)
    return {
        "schema": SCHEMA,
        "verdict": verdict,
        "passed": verdict == "direction_confirmed",
        "usable_cohorts": len(usable),
        "independent_cohorts": independent,
        "mean_ic": round(mean_ic, 4) if mean_ic is not None else None,
        "positive_ic_ratio": round(positive_ratio, 4) if positive_ratio is not None else None,
        "decile_spread": round(mean_spread, 6) if mean_spread is not None else None,
        "cohorts": usable,
        "skipped": skipped,
        "params": dict(PARAMS),
    }


def _verdict(
    usable: Sequence[Mapping[str, Any]],
    independent: int,
    mean_ic: float | None,
    positive_ratio: float | None,
) -> str:
    if not usable or mean_ic is None or positive_ratio is None:
        return "insufficient_sample"
    consistency = float(PARAMS["consistency_ratio"])
    # 方向反了先于独立性判定：即使证据量不足以「确认」，一致的负方向也必须
    # 立刻说出来——它比「没信号」危险得多。
    if mean_ic <= -float(PARAMS["min_abs_mean_ic"]) and (1 - positive_ratio) >= consistency:
        return "direction_inverted"
    if independent < int(PARAMS["min_independent_cohorts"]):
        return "insufficient_independent_cohorts"
    if mean_ic >= float(PARAMS["min_abs_mean_ic"]) and positive_ratio >= consistency:
        return "direction_confirmed"
    return "no_direction"
