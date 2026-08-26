"""纪律分：把"心态不好"变成可跟踪变量（升级方案 P6，源自原书第二十九/三十课）。

原书的处置是：系统外交易必须记录，连续冲动交易触发强制停手，连续大赚同样降低仓位。
方案把它量化成：

    DisciplineScore = 100 − 20·系统外交易 − 10·冲动 − 10·计划外加仓 − 10·追高

    <80  → 次日仓位减半
    <60  → 次日停止实盘，仅模拟
    连续 3 天 <80 → 进入纠错周

与 ``behavior_risk`` 的分工（**不重复扣分**是本模块的硬约束）：
- ``behavior_risk`` 看的是**跨日序列**——连胜后动作扩张、连亏后追损、动作频率漂移、
  策略集中度。它回答"这个账户最近的行为形态是否危险"。
- 本模块看的是**单日执行偏差**——今天有几笔交易脱离了系统。它回答"今天照系统做了吗"。

同一个事件只能进一边。因此本模块只消费**当日**的执行偏差计数，绝不读连胜/连亏；
两者的输出并列呈现，由消费方各自使用。``combined_position_multiplier`` 取二者中
**更保守**的那个倍率，而不是相乘——相乘会让两套独立口径叠加成过度惩罚。

纪律（与仓内其他模块一致）：当日**没有可用执行记录**时返回 ``unavailable`` 而不是
100 分。"今天没有违规"和"今天没有数据"必须可区分——把后者渲染成满分，恰好会在
系统失灵、什么都没记上的那天给出最宽松的仓位。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

AVAILABLE = "available"
UNAVAILABLE = "unavailable"

SCHEMA = "discipline_score_v1"

#: 扣分权重（原书口径）。改动须走正常评审——它直接决定次日仓位。
DEFAULT_PENALTIES: dict[str, int] = {
    "off_system_trade": 20,     # 系统外交易：不在当日建议内的开仓
    "impulsive_trade": 10,      # 冲动交易：无预设触发器
    "unplanned_add": 10,        # 计划外加仓
    "late_chase": 10,           # 追高：成交价高于建议价格区间上沿
}

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "halve_below": 80,          # <80 次日仓位减半
    "paper_only_below": 60,     # <60 次日停实盘
    "correction_week_days": 3,  # 连续 N 天 <80 进纠错周
}

__all__ = ["AVAILABLE", "UNAVAILABLE", "SCHEMA", "DEFAULT_PENALTIES",
           "DEFAULT_THRESHOLDS", "score_day", "next_day_action",
           "needs_correction_week", "combined_position_multiplier"]


def _int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, result)


def score_day(violations: Mapping[str, Any] | None, *,
              executed_trade_count: Any = None,
              penalties: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """当日纪律分。

    ``executed_trade_count`` 是当日实际成交笔数：它区分"今天没有违规"（有交易、
    零违规 → 100 分）与"今天没有数据"（无任何执行记录 → unavailable）。**没有它就
    不能给分**——否则系统失灵、什么都没记上的那天会拿到满分和最宽松的仓位。
    """
    if executed_trade_count is None:
        return {"schema": SCHEMA, "status": UNAVAILABLE, "score": None,
                "reason": "execution_records_missing"}
    weights = {**DEFAULT_PENALTIES, **dict(penalties or {})}
    counts = {name: _int((violations or {}).get(name)) for name in weights}
    deductions = {name: counts[name] * int(weights[name]) for name in weights}
    score = 100 - sum(deductions.values())
    return {
        "schema": SCHEMA,
        "status": AVAILABLE,
        "score": max(0, score),
        "raw_score": score,                 # 未截断值：连续违规的严重程度不该被 0 抹平
        "counts": counts,
        "deductions": deductions,
        "executed_trade_count": _int(executed_trade_count),
        "reason": None,
    }


def next_day_action(score_result: Mapping[str, Any] | None, *,
                    thresholds: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """次日动作。分数不可用时**保守处理**：减半，而不是照常。"""
    cfg = {**DEFAULT_THRESHOLDS, **dict(thresholds or {})}
    result = dict(score_result or {})
    if result.get("status") != AVAILABLE or result.get("score") is None:
        return {"action": "halve_position", "position_multiplier": 0.5,
                "status": UNAVAILABLE,
                "reason": "score_unavailable_conservative_default"}
    score = int(result["score"])
    if score < int(cfg["paper_only_below"]):
        return {"action": "paper_only", "position_multiplier": 0.0,
                "status": AVAILABLE, "reason": f"score<{cfg['paper_only_below']}"}
    if score < int(cfg["halve_below"]):
        return {"action": "halve_position", "position_multiplier": 0.5,
                "status": AVAILABLE, "reason": f"score<{cfg['halve_below']}"}
    return {"action": "normal", "position_multiplier": 1.0,
            "status": AVAILABLE, "reason": None}


def needs_correction_week(recent_scores: Sequence[Any], *,
                          thresholds: Mapping[str, Any] | None = None
                          ) -> dict[str, Any]:
    """连续 N 天低于 halve_below → 进入纠错周。

    只看**最近连续**的那一段：中间有一天回到 80 以上，连续性就断了。历史上更早的
    低分日不该无限期累积成惩罚。
    """
    cfg = {**DEFAULT_THRESHOLDS, **dict(thresholds or {})}
    needed = int(cfg["correction_week_days"])
    values = [value for value in (recent_scores or ()) if value is not None]
    if not values:
        return {"status": UNAVAILABLE, "needed": needed, "streak": 0,
                "triggered": None, "reason": "no_score_history"}
    streak = 0
    for value in reversed(values):          # 从最近一天往回数
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            break
        if numeric < int(cfg["halve_below"]):
            streak += 1
        else:
            break
    return {"status": AVAILABLE, "needed": needed, "streak": streak,
            "triggered": streak >= needed, "reason": None}


def combined_position_multiplier(discipline_action: Mapping[str, Any] | None,
                                 behavior_multiplier: Any = None) -> dict[str, Any]:
    """纪律分与 behavior_risk 的仓位倍率合并。

    取**更保守**的一个，不相乘——两者是相互独立的口径（单日执行偏差 vs 跨日行为
    形态），相乘会把同一个坏日子惩罚两次。
    """
    action = dict(discipline_action or {})
    discipline_mult = action.get("position_multiplier")
    candidates = [value for value in (discipline_mult, behavior_multiplier)
                  if value is not None]
    if not candidates:
        return {"status": UNAVAILABLE, "position_multiplier": None,
                "reason": "no_multiplier_available"}
    chosen = min(float(value) for value in candidates)
    return {
        "status": AVAILABLE,
        "position_multiplier": round(chosen, 4),
        "source": ("discipline" if discipline_mult is not None
                   and float(discipline_mult) == chosen else "behavior_risk"),
        "inputs": {"discipline": discipline_mult, "behavior_risk": behavior_multiplier},
        "reason": None,
    }
