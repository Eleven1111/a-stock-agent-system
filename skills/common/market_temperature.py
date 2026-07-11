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


def classify_tier(height: int, promo: Optional[float]) -> Dict[str, Any]:
    """五档判定（纯函数）。晋级率缺失时按高度板单变量保守降一档。"""
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
    return {"tier": tier, "notes": notes}


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
                        ) -> Dict[str, Any]:
    """完整温度计（纯函数）。数据缺失时阻断新风险。"""
    if not ladder:
        return _unavailable_temperature(
            "unknown",
            "lianban_ladder 缺失",
            limitup_total=limitup_total,
        )

    height = ladder_height(ladder)
    promo = promotion_rate(ladder, prev_ladder)
    cls = classify_tier(height, promo)
    tier = cls["tier"]
    notes = cls["notes"]
    rules = dict(TIER_RULES[tier])

    retreat = detect_retreat(retreat_ladder or prev_ladder, morning_quotes)
    if retreat:
        rules["allow_new_daban"] = False
        rules["position_multiplier"] = 0.0
        rules["top_n_limit"] = 0
        rules["advice"] = f"退潮信号触发：{retreat}｜只出不进"
        notes.append(retreat)

    return {
        "tier": tier,
        "context_status": "fresh",
        "height": height,
        "promotion_rate": promo,
        "limitup_total": limitup_total,
        "retreat_signal": retreat,
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
        "limitup_total": limitup_total,
        "allow_new_daban": False,
        "position_multiplier": 0.0,
        "top_n_limit": 0,
        "retreat_signal": None,
        "advice": f"温度上下文{status}，阻断新增风险",
        "context_asof": context_asof,
        "context_fresh": False,
        "notes": [reason],
    }


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
        limitup_total=context.get("limitup_total"),
        morning_quotes=morning_quotes,
        retreat_ladder=context.get("lianban_ladder") if morning_quotes else None,
    )
    result.update({
        "context_asof": context_asof or None,
        "context_fresh": True,
        "context_status": "fresh",
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
