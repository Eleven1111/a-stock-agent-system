#!/usr/bin/env python3
"""
市场情绪温度计 — 高度板 × 连板晋级率 → 五档情绪定位
====================================================
游资方法论的共同核心（《游资选股》两份深度研究报告）：超短先选"情绪位置"再选股。
五档：冰点 → 修复 → 发酵 → 加速 → 极热。核心入场区是修复后期~发酵期；
加速期只做最强；极热与冰点只出不进/只观察。

量化口径（综合两报告）：
- 高度板 = 当日最高连板数（来自 signal_context.lianban_ladder）
- 连板晋级率 = 昨日涨停票中今日再封板(lianban>=2)的比例（需昨日梯队快照）
- 退潮硬信号 = 昨日高度板今晨跌幅 < -5%，或昨日涨停大面积低开 → 无论档位强制只出不进

输出操作约束（被 candidate_discovery 排名 gate 与 recommendation_audit 仓位消费）：
- allow_new_daban：是否允许新开打板仓
- position_multiplier：仓位倍率（报告：牛市6-8成 vs 弱市≤3成的环境适配）
- top_n_limit：当日最多参与的打板候选数（加速期只做最强=1）

数据缺失、过期或异常时输出 unknown/stale 一等状态，并将新风险预算归零；
不能把没有证据解释为 neutral。纯标准库，cron-safe。
"""

import os
import sys
from datetime import datetime
from math import sqrt
from statistics import mean, pstdev
from typing import Any, Dict, List, Mapping, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from signal_context import read_signal_context  # noqa: E402

TIER_RULES = {
    "冰点": {"allow_new_daban": True, "position_multiplier": 0.3, "top_n_limit": 2,
             "advice": "轻仓聚焦板块龙头，只做强势板块"},
    "修复": {"allow_new_daban": True, "position_multiplier": 0.6, "top_n_limit": 2,
             "advice": "小仓试错，优先首板龙一"},
    "发酵": {"allow_new_daban": True, "position_multiplier": 1.0, "top_n_limit": 5,
             "advice": "游资核心入场区"},
    "加速": {"allow_new_daban": True, "position_multiplier": 0.8, "top_n_limit": 1,
             "advice": "只做最强，持仓享受溢价"},
    "极热": {"allow_new_daban": False, "position_multiplier": 0.0, "top_n_limit": 0,
             "advice": "只卖不买，防退潮"},
}

# 打板战略权重：打板可成交 edge 已被 2 年全市场 OOS 证伪(issue #28)，打板范式整体降配、
# 重心移向 trend(1+2 定位决策)。此权重在情绪温度倍率之上再乘——温度是择时，此处是战略再平衡。
# 默认 0.5(温和减半)；HERMES_DABAN_STRATEGIC_WEIGHT 可覆盖(0~1)。trend/中线策略不受影响。
DABAN_STRATEGIC_WEIGHT_DEFAULT = 0.5

TIER_ORDER = ("冰点", "修复", "发酵", "加速", "极热")


def _number(value: Any) -> Optional[float]:
    """Coerce a market statistic without turning missing data into zero."""
    if isinstance(value, bool) or value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def three_day_slope(values: Any) -> Optional[float]:
    """Return the per-day slope of the latest three observations.

    The function intentionally accepts a short history and returns ``None``
    when it cannot establish a three-day trend.  This keeps a missing market
    breadth series from being silently interpreted as a flat series.
    """
    if not isinstance(values, (list, tuple)):
        return None
    points = [_number(value) for value in values[-3:]]
    if len(points) < 3 or any(value is None for value in points):
        return None
    first, middle, last = (float(value) for value in points)
    # Least-squares slope for x = 0,1,2; equivalent to (last-first)/2,
    # while being explicit about the meaning of "3 日斜率".
    return round((last - first) / 2.0, 4)


def smooth_three_day(values: Any) -> Optional[float]:
    """Three-observation moving average, or ``None`` when unavailable."""
    if not isinstance(values, (list, tuple)):
        return None
    points = [_number(value) for value in values[-3:]]
    if len(points) < 3 or any(value is None for value in points):
        return None
    return round(mean(float(value) for value in points), 4)


def _history_series(history: Any, keys: tuple[str, ...]) -> list[Optional[float]]:
    if not isinstance(history, (list, tuple)):
        return []
    values: list[Optional[float]] = []
    for row in history:
        if not isinstance(row, Mapping):
            continue
        value = None
        for key in keys:
            value = _number(row.get(key))
            if value is not None:
                break
        values.append(value)
    return values


def premium_statistics(values: Any) -> Dict[str, Any]:
    """Point estimate and 95% CI for executable next-day net premium."""
    if not isinstance(values, (list, tuple)):
        return {"value": None, "confidence_interval": None, "sample_size": 0}
    samples = [float(value) for value in (_number(item) for item in values)
               if value is not None]
    if not samples:
        return {"value": None, "confidence_interval": None, "sample_size": 0}
    estimate = mean(samples)
    margin = 1.96 * pstdev(samples) / sqrt(len(samples)) if len(samples) > 1 else None
    interval = (
        [round(estimate - margin, 4), round(estimate + margin, 4)]
        if margin is not None else None
    )
    return {
        "value": round(estimate, 4),
        "confidence_interval": interval,
        "sample_size": len(samples),
    }


def _premium_history_by_tier(history: Any) -> Dict[str, List[Any]]:
    """Normalize either tier->samples or row-oriented premium history."""
    if isinstance(history, Mapping):
        return {str(key): list(value) if isinstance(value, (list, tuple)) else []
                for key, value in history.items()}
    grouped: Dict[str, List[Any]] = {}
    if isinstance(history, (list, tuple)):
        for row in history:
            if not isinstance(row, Mapping):
                continue
            tier = str(row.get("tier") or row.get("state") or "")
            value = (row.get("next_day_net_premium")
                     if row.get("next_day_net_premium") is not None
                     else row.get("next_day_premium", row.get("premium")))
            if tier and value is not None:
                grouped.setdefault(tier, []).append(value)
    return grouped


def _temperature_metrics(
    market_history: Any = None,
    *,
    limitup_total: Optional[int] = None,
    broken_rate: Optional[float] = None,
    previous_ladder_premium: Optional[float] = None,
    limitdown_total: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the P1 breadth/feedback overlay from a bounded history."""
    history = list(market_history or []) if isinstance(market_history, (list, tuple)) else []
    series = {
        "limitup_total": _history_series(history, ("limitup_total", "limitups", "涨停数")),
        "broken_rate": _history_series(history, ("broken_rate", "炸板率", "break_rate")),
        "previous_ladder_premium": _history_series(
            history, ("previous_ladder_premium", "yesterday_limitup_premium", "昨日涨停溢价")
        ),
        "limitdown_total": _history_series(history, ("limitdown_total", "limitdowns", "跌停数")),
    }
    current = {
        "limitup_total": limitup_total,
        "broken_rate": broken_rate,
        "previous_ladder_premium": previous_ladder_premium,
        "limitdown_total": limitdown_total,
    }
    for key, value in current.items():
        number = _number(value)
        if number is not None:
            series[key].append(number)
    result: Dict[str, Any] = {}
    for key, values in series.items():
        result[f"{key}_3d_slope"] = three_day_slope(values)
        result[f"{key}_3d_smooth"] = smooth_three_day(values)
        result[key] = values[-1] if values else None
    # Expose the Chinese report vocabulary as stable aliases for consumers.
    result["limitup_slope_3d"] = result["limitup_total_3d_slope"]
    result["broken_rate_slope_3d"] = result["broken_rate_3d_slope"]
    result["yesterday_limitup_premium"] = result["previous_ladder_premium"]
    result["yesterday_limitup_premium_3d_slope"] = result[
        "previous_ladder_premium_3d_slope"
    ]
    result["limitdown_count"] = result["limitdown_total"]
    result["limitdown_slope_3d"] = result["limitdown_total_3d_slope"]
    return result


def _ice_substate(metrics: Mapping[str, Any]) -> str:
    """Separate a falling ice point from a confirmed, tradable repair."""
    up = _number(metrics.get("limitup_total_3d_slope"))
    broken = _number(metrics.get("broken_rate_3d_slope"))
    premium = _number(metrics.get("previous_ladder_premium"))
    down = _number(metrics.get("limitdown_total_3d_slope"))
    # With no breadth/feedback history there is no honest way to call the
    # point either a selloff or a confirmed repair.  Keep the legacy coarse
    # tier in that case; callers with an explicit history always fail closed.
    if up is None or broken is None or premium is None or down is None:
        return ""
    repaired = up > 0 and broken <= 0 and premium >= 0 and down <= 0
    return "冰点修复" if repaired else "冰点杀跌"


def daban_strategic_weight() -> float:
    """打板战略减仓权重(0~1，默认 0.5)。环境变量 HERMES_DABAN_STRATEGIC_WEIGHT 覆盖，非法值回退默认。"""
    raw = os.environ.get("HERMES_DABAN_STRATEGIC_WEIGHT")
    if raw is not None:
        try:
            value = float(raw)
        except ValueError:
            return DABAN_STRATEGIC_WEIGHT_DEFAULT
        if 0.0 <= value <= 1.0:
            return value
    return DABAN_STRATEGIC_WEIGHT_DEFAULT


def ladder_height(ladder: Optional[Mapping[str, Any]]) -> int:
    """当日最高连板数（高度板）。"""
    if not ladder:
        return 0
    best = 0
    for entry in ladder.values():
        if isinstance(entry, Mapping):
            best = max(best, int(entry.get("lianban") or 0))
    return best


def promotion_rate(ladder: Optional[Mapping[str, Any]],
                   prev_ladder: Optional[Mapping[str, Any]]) -> Optional[float]:
    """连板晋级率 = 昨日涨停票今日再封板(lianban>=2)的比例。无昨日快照返回 None。"""
    if not prev_ladder:
        return None
    prev_codes = set(prev_ladder.keys())
    if not prev_codes:
        return None
    today = ladder or {}
    promoted = sum(
        1 for code in prev_codes
        if isinstance(today.get(code), Mapping) and int(today[code].get("lianban") or 0) >= 2
    )
    return round(promoted / len(prev_codes), 4)


def classify_tier(height: int, promo: Optional[float],
                  previous_tier: Optional[str] = None) -> Dict[str, Any]:
    """五档判定（纯函数），支持单档滞回避免边界来回跳转。"""
    notes: List[str] = []
    if promo is None:
        notes.append("晋级率缺失（无昨日梯队快照），按高度板保守判定")
        if height >= 8:
            tier = "极热"
        elif height >= 6:
            tier = "发酵"   # 缺晋级率不敢判加速，保守
        elif height >= 4:
            tier = "修复"
        else:
            tier = "冰点"
    else:
        if height >= 8 or promo >= 0.70:
            tier = "极热"
        elif height >= 6 and promo >= 0.50:
            tier = "加速"
        elif height >= 4 and promo >= 0.35:
            tier = "发酵"
        elif height >= 3 and promo >= 0.20:
            tier = "修复"
        else:
            tier = "冰点"
    raw_tier = tier
    if previous_tier in TIER_ORDER and tier in TIER_ORDER:
        previous_index = TIER_ORDER.index(previous_tier)
        current_index = TIER_ORDER.index(tier)
        # A one-step change at a boundary needs confirmation from both
        # dimensions; otherwise keep the previous state for one observation.
        if abs(current_index - previous_index) == 1:
            strong_up = promo is not None and promo >= {
                "修复": 0.30, "发酵": 0.45, "加速": 0.60, "极热": 0.80,
            }.get(tier, 1.0)
            strong_down = promo is not None and promo < {
                "冰点": 0.10, "修复": 0.15, "发酵": 0.30, "加速": 0.45,
            }.get(tier, 0.0)
            if not (strong_up or strong_down or height >= 8):
                notes.append(f"状态滞回：{previous_tier}→{tier}证据不足，保持{previous_tier}")
                tier = previous_tier
    return {"tier": tier, "raw_tier": raw_tier, "notes": notes}


def detect_retreat(prev_ladder: Optional[Mapping[str, Any]],
                   morning_quotes: Optional[Mapping[str, Mapping[str, Any]]],
                   height_drop_pct: float = -5.0,
                   broad_low_open_ratio: float = 0.6) -> Optional[str]:
    """退潮硬信号（盘中修正，morning_quotes: code -> {change_pct}）：
    昨日高度板今晨跌超 5%，或昨日涨停票低开比例过高。无数据返回 None。"""
    if not prev_ladder or not morning_quotes:
        return None
    entries = [(c, e) for c, e in prev_ladder.items() if isinstance(e, Mapping)]
    if not entries:
        return None
    normalized_quotes = {
        str(code).lower().removeprefix("sh").removeprefix("sz").zfill(6): quote
        for code, quote in morning_quotes.items()
        if isinstance(quote, Mapping)
    }

    def _morning_pct(code: str) -> Optional[float]:
        quote = normalized_quotes.get(
            str(code).lower().removeprefix("sh").removeprefix("sz").zfill(6)
        )
        if not isinstance(quote, Mapping):
            return None
        gap = quote.get("auction_gap_pct")
        if isinstance(gap, (int, float)):
            return float(gap)
        open_price = quote.get("open")
        prev_close = quote.get("prev_close")
        if (
            isinstance(open_price, (int, float))
            and isinstance(prev_close, (int, float))
            and prev_close > 0
        ):
            return (float(open_price) / float(prev_close) - 1.0) * 100
        change_pct = quote.get("change_pct")
        return float(change_pct) if isinstance(change_pct, (int, float)) else None

    max_lb = max(int(e.get("lianban") or 0) for _, e in entries)
    height_codes = [c for c, e in entries if int(e.get("lianban") or 0) == max_lb]
    for code in height_codes:
        pct = _morning_pct(code)
        if pct is not None and pct <= height_drop_pct:
            return f"昨日高度板{code}({max_lb}板)今晨{pct:+.1f}%，退潮硬信号"
    observed = [
        pct for code, _ in entries
        if (pct := _morning_pct(code)) is not None
    ]
    if len(observed) >= 5:
        low_open = sum(1 for pct in observed if pct < 0)
        if low_open / len(observed) >= broad_low_open_ratio:
            return f"昨日涨停{len(observed)}只中{low_open}只低开，普遍弱反馈"
    return None


def compute_temperature(ladder: Optional[Mapping[str, Any]],
                        prev_ladder: Optional[Mapping[str, Any]] = None,
                        limitup_total: Optional[int] = None,
                        morning_quotes: Optional[Mapping[str, Mapping[str, Any]]] = None,
                        retreat_ladder: Optional[Mapping[str, Any]] = None,
                        market_history: Optional[List[Mapping[str, Any]]] = None,
                        limitup_history: Optional[List[Any]] = None,
                        broken_rate: Optional[float] = None,
                        limitdown_total: Optional[int] = None,
                        previous_ladder_premium: Optional[float] = None,
                        next_day_premium_history: Any = None,
                        previous_tier: Optional[str] = None,
                        history: Optional[List[Mapping[str, Any]]] = None,
                        broken_rate_history: Optional[List[Any]] = None,
                        limitdown_history: Optional[List[Any]] = None,
                        yesterday_premium_history: Optional[List[Any]] = None,
                        ) -> Dict[str, Any]:
    """完整温度计（纯函数）。数据缺失时阻断新风险。

    ``morning_quotes`` 区分两种语义，不可混为一谈：
    - ``None``：调用方本就不做盘中修正（开盘前批次），退潮检查 not_requested；
    - 空映射：调用方请求了盘中修正却拿到零观测（竞价采集失败，issue #112/#113），
      此时"未检出退潮"是没有证据而非有证据，必须 fail closed 而不是沿用档位风险预算。
    """
    if not ladder:
        return _unavailable_temperature(
            "unknown",
            "lianban_ladder 缺失",
            limitup_total=limitup_total,
        )

    height = ladder_height(ladder)
    promo = promotion_rate(ladder, prev_ladder)
    market_history = market_history or history
    if market_history is None and any(value is not None for value in (
        limitup_history, broken_rate_history, limitdown_history,
        yesterday_premium_history,
    )):
        series = (
            list(limitup_history or []), list(broken_rate_history or []),
            list(yesterday_premium_history or []), list(limitdown_history or []),
        )
        market_history = [
            {
                key: values[index] for key, values in zip(
                    ("limitup_total", "broken_rate", "previous_ladder_premium", "limitdown_total"),
                    series
                ) if index < len(values)
            }
            for index in range(max((len(values) for values in series), default=0))
        ]
    metrics = _temperature_metrics(
        market_history,
        limitup_total=limitup_total,
        broken_rate=broken_rate,
        limitdown_total=limitdown_total,
        previous_ladder_premium=previous_ladder_premium,
    )
    cls = classify_tier(height, promo, previous_tier=previous_tier)
    tier = cls["tier"]
    notes = cls["notes"]
    rules = dict(TIER_RULES[tier])

    ice_substate = _ice_substate(metrics) if tier == "冰点" else None
    ice_substate = ice_substate or None
    if ice_substate == "冰点杀跌":
        rules.update(allow_new_daban=False, position_multiplier=0.0, top_n_limit=0)
        rules["advice"] = "冰点杀跌，停止新增仓位｜只出不进"
    elif ice_substate == "冰点修复":
        rules.update(allow_new_daban=True, position_multiplier=0.2, top_n_limit=1)
        rules["advice"] = "冰点修复已确认，仅允许小仓试错"

    premium_samples_by_tier = _premium_history_by_tier(next_day_premium_history)
    premium_by_state = {
        state: premium_statistics(premium_samples_by_tier.get(state))
        for state in TIER_ORDER
    }
    premium_input = premium_samples_by_tier.get(tier)
    if not premium_samples_by_tier and isinstance(next_day_premium_history, (list, tuple)):
        premium_input = next_day_premium_history
    premium_stats = premium_statistics(premium_input)
    metrics.update({
        "ice_substate": ice_substate,
        "next_day_net_premium": premium_stats["value"],
        "next_day_net_premium_ci": premium_stats["confidence_interval"],
        "next_day_executable_net_premium": premium_stats["value"],
        "next_day_executable_net_premium_ci": premium_stats["confidence_interval"],
        "next_day_net_premium_confidence_interval": premium_stats["confidence_interval"],
        "net_premium_confidence_interval": premium_stats["confidence_interval"],
        "premium_sample_size": premium_stats["sample_size"],
    })

    observation_missing = morning_quotes is not None and not morning_quotes
    retreat = detect_retreat(retreat_ladder or prev_ladder, morning_quotes)
    if retreat:
        rules["allow_new_daban"] = False
        rules["position_multiplier"] = 0.0
        rules["top_n_limit"] = 0
        rules["advice"] = f"退潮信号触发：{retreat}｜只出不进"
        notes.append(retreat)
    elif observation_missing:
        rules["allow_new_daban"] = False
        rules["position_multiplier"] = 0.0
        rules["top_n_limit"] = 0
        rules["advice"] = "盘中观测缺失，无法确认退潮与否｜阻断新增风险"
        notes.append("盘中观测缺失（竞价快照为空），退潮检查未执行，按无证据处理")

    if morning_quotes is None:
        retreat_check = "not_requested"
    elif observation_missing:
        retreat_check = "observation_missing"
    else:
        retreat_check = "checked"

    return {
        "tier": tier,
        "context_status": "degraded" if observation_missing else "fresh",
        "height": height,
        "promotion_rate": promo,
        "limitup_total": limitup_total,
        "ice_substate": ice_substate,
        "market_metrics": metrics,
        "limitup_slope_3d": metrics.get("limitup_slope_3d"),
        "broken_rate_slope_3d": metrics.get("broken_rate_slope_3d"),
        "yesterday_limitup_premium": metrics.get("yesterday_limitup_premium"),
        "limitdown_slope_3d": metrics.get("limitdown_slope_3d"),
        "limitup_3d_smooth": metrics.get("limitup_total_3d_smooth"),
        "broken_rate_3d_smooth": metrics.get("broken_rate_3d_smooth"),
        "next_day_net_premium": premium_stats["value"],
        "next_day_net_premium_ci": premium_stats["confidence_interval"],
        "next_day_executable_net_premium": premium_stats["value"],
        "next_day_executable_net_premium_ci": premium_stats["confidence_interval"],
        "next_day_net_premium_confidence_interval": premium_stats["confidence_interval"],
        "net_premium_confidence_interval": premium_stats["confidence_interval"],
        "premium_by_state": premium_by_state,
        "retreat_signal": retreat,
        "retreat_check": retreat_check,
        "notes": notes,
        **rules,
    }


def _unavailable_temperature(
    status: str,
    reason: str,
    context_asof: Optional[str] = None,
    limitup_total: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "tier": status,
        "context_status": status,
        "height": 0,
        "promotion_rate": None,
        "ice_substate": None,
        "market_metrics": {},
        "next_day_net_premium": None,
        "next_day_net_premium_ci": None,
        "next_day_net_premium_confidence_interval": None,
        "net_premium_confidence_interval": None,
        "next_day_executable_net_premium": None,
        "next_day_executable_net_premium_ci": None,
        "premium_by_state": {},
        "limitup_total": limitup_total,
        "allow_new_daban": False,
        "position_multiplier": 0.0,
        "top_n_limit": 0,
        "retreat_signal": None,
        "retreat_check": "not_requested",
        "advice": f"温度上下文{status}，阻断新增风险",
        "context_asof": context_asof,
        "context_fresh": False,
        "notes": [reason],
    }


def block_new_risk(
    temperature: Mapping[str, Any],
    reason: str,
    *,
    status: str = "degraded",
) -> Dict[str, Any]:
    """在既有温度结论上叠加一层阻断：归零新增风险预算，保留档位与诊断信息。

    供消费方在"温度本身算得出来，但支撑它的观测不可信"时使用（如竞价短名单
    降级）。刻意不改写 tier —— 把档位抹成 unknown 会丢掉排查线索，真正要
    失效的是风险预算而非诊断。
    """
    blocked = dict(temperature)
    blocked.update({
        "context_status": status,
        "context_fresh": False,
        "allow_new_daban": False,
        "position_multiplier": 0.0,
        "top_n_limit": 0,
        "advice": f"{reason}｜阻断新增风险",
        "notes": [*(temperature.get("notes") or []), reason],
    })
    return blocked


def temperature_from_context(
    ctx: Optional[Mapping[str, Any]],
    morning_quotes: Optional[Mapping[str, Mapping[str, Any]]] = None,
    event_asof: Optional[str] = None,
    max_age_days: int = 4,
) -> Dict[str, Any]:
    """计算带日期门禁的温度；过期/未来/无日期缓存一律阻断新风险。"""
    context = dict(ctx or {})
    context_asof = str(context.get("ladder_asof") or "")
    ladder = context.get("lianban_ladder")
    if not ladder:
        return _unavailable_temperature("unknown", "lianban_ladder 缺失", context_asof or None)
    if event_asof:
        try:
            event_day = datetime.fromisoformat(str(event_asof)).date()
            context_day = datetime.fromisoformat(context_asof).date()
        except ValueError:
            return _unavailable_temperature(
                "unknown", "ladder_asof 缺失或无效", context_asof or None,
            )
        age_days = (event_day - context_day).days
        if age_days < 0:
            return _unavailable_temperature(
                "unknown",
                f"情绪上下文来自未来日期: {context_asof}",
                context_asof,
            )
        if age_days > max_age_days:
            return _unavailable_temperature(
                "stale",
                f"情绪上下文已过期: {context_asof}，距事件日{age_days}天",
                context_asof,
            )

    result = compute_temperature(
        ladder=context.get("lianban_ladder"),
        prev_ladder=context.get("prev_lianban_ladder"),
        limitup_total=(context.get("limitup_total")
                       if context.get("limitup_total") is not None
                       else (context.get("market_sentiment") or {}).get("limitup_total")),
        morning_quotes=morning_quotes,
        retreat_ladder=context.get("lianban_ladder") if morning_quotes else None,
        market_history=(context.get("market_history")
                        or context.get("sentiment_history")
                        or context.get("market_sentiment_history")
                        or (context.get("market_sentiment") or {}).get("history")),
        limitup_history=context.get("limitup_history"),
        broken_rate_history=context.get("broken_rate_history"),
        limitdown_history=context.get("limitdown_history"),
        yesterday_premium_history=(context.get("yesterday_premium_history")
                                   or context.get("limitup_premium_history")),
        broken_rate=(context.get("broken_rate")
                     if context.get("broken_rate") is not None
                     else context.get("炸板率")
                     if context.get("炸板率") is not None
                     else (context.get("market_sentiment") or {}).get("broken_rate")),
        limitdown_total=(context.get("limitdown_total")
                         if context.get("limitdown_total") is not None
                         else context.get("limitdown_count")
                         if context.get("limitdown_count") is not None
                         else (context.get("market_sentiment") or {}).get("limitdown_total")),
        previous_ladder_premium=(context.get("previous_ladder_premium")
                                 if context.get("previous_ladder_premium") is not None
                                 else context.get("yesterday_limitup_premium")
                                 if context.get("yesterday_limitup_premium") is not None
                                 else (context.get("market_sentiment") or {}).get("previous_ladder_premium")),
        next_day_premium_history=(context.get("next_day_premium_history")
                                  or context.get("premium_history")),
        previous_tier=context.get("previous_tier") or context.get("temperature_tier"),
    )
    degraded = result.get("context_status") == "degraded"
    result.update({
        "context_asof": context_asof or None,
        # 上下文日期新鲜不代表观测完整：盘中观测缺失时不得回填 fresh
        "context_fresh": not degraded,
        "context_status": "degraded" if degraded else "fresh",
    })
    return result


def read_temperature(
    morning_quotes: Optional[Mapping[str, Mapping[str, Any]]] = None,
    event_asof: Optional[str] = None,
    max_age_days: int = 4,
) -> Dict[str, Any]:
    """从 signal_context 读取温度；缺失、异常或日期不可信时阻断新风险。"""
    try:
        context = read_signal_context(max_age_hours=max(24, max_age_days * 24)) or {}
    except (OSError, RuntimeError, TimeoutError) as exc:
        return _unavailable_temperature(
            "unknown", f"情绪上下文读取失败: {exc}",
        )
    return temperature_from_context(
        context,
        morning_quotes=morning_quotes,
        event_asof=event_asof,
        max_age_days=max_age_days,
    )


# ────────────────────────────────────────────────────────────────────────────
# S0-S6 概率状态机（游资方法论报告第六章）
# 五档温度是它的离散骨架；这里叠加拥挤/脆弱/板块轮动/广度证据，细化到七态并输出
# 概率而非硬标签，再用滞后(SWITCH_MARGIN)避免单日证据让主导状态来回翻转。
# 纯标准库启发式映射 —— 无训练数据、无 hmmlearn，是工程约束下的可解释近似，
# 不假装是校准过的 HMM/HSMM（报告第十章："不能复刻也不应假装复刻"）。
# ────────────────────────────────────────────────────────────────────────────

MARKET_STATES = ("S0", "S1", "S2", "S3", "S4", "S5", "S6")
STATE_LABELS = {
    "S0": "收缩/冰点", "S1": "修复", "S2": "点火", "S3": "扩散/主升",
    "S4": "高潮/拥挤", "S5": "分歧/轮动", "S6": "退潮/级联",
}
STATE_ACTION = {
    "S0": "ABSTAIN/观察", "S1": "小仓 TEST", "S2": "建池识龙头",
    "S3": "确认后加风险预算", "S4": "停止追一致/准备 REDUCE",
    "S5": "区分换手/切换", "S6": "INVALIDATE/降暴露",
}
# 五档 → 基础状态（neutral 不映射，状态机不输出方向性状态）
TIER_TO_STATE = {"冰点": "S0", "修复": "S1", "发酵": "S2", "加速": "S3", "极热": "S4"}
# 主导状态属于这些时，decision_policy/上游按 risk_off 处理
STATE_RISK_OFF = {"S0", "S6"}
# 新主导状态相对上一状态的概率优势不足此值则不切换（滞后，防单 K 翻转）
SWITCH_MARGIN = 0.15


def _coerce_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None


def classify_market_state(
    temperature: Mapping[str, Any],
    *,
    breadth: Optional[Mapping[str, Any]] = None,
    crowding_score: Optional[float] = None,
    fragility_score: Optional[float] = None,
    sector_rotation: Optional[Mapping[str, Any]] = None,
    previous_state: Optional[str] = None,
) -> Dict[str, Any]:
    """五档温度 + 拥挤/脆弱/轮动/广度证据 → S0-S6 概率分布与主导状态。"""
    tier = str(temperature.get("tier") or "")
    base = TIER_TO_STATE.get(tier)
    if base is None:
        return {
            "schema": "market_state_machine_v1",
            "available": False,
            "calibrated": False,
            "market_state_prob": {},
            "dominant_state": None,
            "previous_state": previous_state,
            "switched": False,
            "context_status": temperature.get("context_status") or "unknown",
            "risk_off": True,
            "notes": ["温度数据缺失、过期或未知，状态机不输出方向性状态"],
        }

    order = list(MARKET_STATES)
    base_idx = order.index(base)
    scores = {state: {0: 1.0, 1: 0.35, 2: 0.1}.get(abs(idx - base_idx), 0.0)
              for idx, state in enumerate(order)}

    if temperature.get("retreat_signal"):
        scores["S6"] += 0.8
    if fragility_score is not None:
        scores["S6"] += 0.5 * float(fragility_score)
    if crowding_score is not None:
        scores["S4"] += 0.5 * float(crowding_score)

    rotation = dict(sector_rotation or {})
    weakening = _coerce_float(rotation.get("weakening_ratio"))
    emerging = _coerce_float(rotation.get("emerging_ratio"))
    if weakening is not None and emerging is not None and weakening >= 0.34 and emerging > 0:
        scores["S5"] += 0.6

    b = dict(breadth or {})
    limitdown = _coerce_float(b.get("limitdown_count")) or 0.0
    limitup = _coerce_float(b.get("limitup_count")) or 0.0
    if limitdown >= max(5.0, limitup):
        scores["S0"] += 0.4
        scores["S6"] += 0.3

    total = sum(scores.values())
    prob = {state: round(score / total, 4) for state, score in scores.items()} if total > 0 else {}
    raw_dominant = max(prob, key=prob.get) if prob else None

    dominant = raw_dominant
    switched = raw_dominant != previous_state
    if previous_state in prob and raw_dominant != previous_state:
        if prob[raw_dominant] - prob.get(previous_state, 0.0) < SWITCH_MARGIN:
            dominant = previous_state  # 滞后：优势不足不切换
            switched = False

    return {
        "schema": "market_state_machine_v1",
        "available": True,
        "calibrated": False,
        "market_state_prob": prob,
        "dominant_state": dominant,
        "dominant_label": STATE_LABELS.get(dominant),
        "dominant_action": STATE_ACTION.get(dominant),
        "raw_dominant_state": raw_dominant,
        "previous_state": previous_state,
        "switched": switched,
        "confidence": prob.get(dominant) if prob else None,
        "risk_off": dominant in STATE_RISK_OFF,
        "notes": [],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(read_temperature(), ensure_ascii=False, indent=2))
