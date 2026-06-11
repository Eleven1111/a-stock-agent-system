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

数据缺失时回退 neutral（multiplier=1.0、不限制）——温度计缺数据不应瘫痪系统，
但会在 notes 标明"温度数据缺失"。纯标准库，cron-safe。
"""

import os
import sys
from typing import Any, Dict, List, Mapping, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from signal_context import read_signal_context  # noqa: E402

TIER_RULES = {
    "冰点": {"allow_new_daban": False, "position_multiplier": 0.3, "top_n_limit": 0,
             "advice": "只观察，不追高"},
    "修复": {"allow_new_daban": True, "position_multiplier": 0.6, "top_n_limit": 2,
             "advice": "小仓试错，优先首板龙一"},
    "发酵": {"allow_new_daban": True, "position_multiplier": 1.0, "top_n_limit": 5,
             "advice": "游资核心入场区"},
    "加速": {"allow_new_daban": True, "position_multiplier": 0.8, "top_n_limit": 1,
             "advice": "只做最强，持仓享受溢价"},
    "极热": {"allow_new_daban": False, "position_multiplier": 0.0, "top_n_limit": 0,
             "advice": "只卖不买，防退潮"},
}


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
    max_lb = max(int(e.get("lianban") or 0) for _, e in entries)
    height_codes = [c for c, e in entries if int(e.get("lianban") or 0) == max_lb]
    for code in height_codes:
        q = morning_quotes.get(code)
        if isinstance(q, Mapping) and isinstance(q.get("change_pct"), (int, float)):
            if q["change_pct"] <= height_drop_pct:
                return f"昨日高度板{code}({max_lb}板)今晨{q['change_pct']:+.1f}%，退潮硬信号"
    observed = [
        q["change_pct"] for c, _ in entries
        if isinstance((q := morning_quotes.get(c)), Mapping)
        and isinstance(q.get("change_pct"), (int, float))
    ]
    if len(observed) >= 5:
        low_open = sum(1 for pct in observed if pct < 0)
        if low_open / len(observed) >= broad_low_open_ratio:
            return f"昨日涨停{len(observed)}只中{low_open}只低开，普遍弱反馈"
    return None


def compute_temperature(ladder: Optional[Mapping[str, Any]],
                        prev_ladder: Optional[Mapping[str, Any]] = None,
                        limitup_total: Optional[int] = None,
                        morning_quotes: Optional[Mapping[str, Mapping[str, Any]]] = None
                        ) -> Dict[str, Any]:
    """完整温度计（纯函数）。数据缺失 → neutral 回退，不瘫痪下游。"""
    if not ladder:
        return {"tier": "neutral", "height": 0, "promotion_rate": None,
                "limitup_total": limitup_total, "allow_new_daban": True,
                "position_multiplier": 1.0, "top_n_limit": None, "retreat_signal": None,
                "advice": "温度数据缺失，不施加情绪约束",
                "notes": ["lianban_ladder 缺失"]}

    height = ladder_height(ladder)
    promo = promotion_rate(ladder, prev_ladder)
    cls = classify_tier(height, promo)
    tier = cls["tier"]
    notes = cls["notes"]
    rules = dict(TIER_RULES[tier])

    retreat = detect_retreat(prev_ladder, morning_quotes)
    if retreat:
        rules["allow_new_daban"] = False
        rules["position_multiplier"] = 0.0
        rules["top_n_limit"] = 0
        rules["advice"] = f"退潮信号触发：{retreat}｜只出不进"
        notes.append(retreat)

    return {
        "tier": tier,
        "height": height,
        "promotion_rate": promo,
        "limitup_total": limitup_total,
        "retreat_signal": retreat,
        "notes": notes,
        **rules,
    }


def read_temperature(morning_quotes: Optional[Mapping[str, Mapping[str, Any]]] = None
                     ) -> Dict[str, Any]:
    """从 signal_context 缓存读取梯队并计算温度。缓存缺失 → neutral。"""
    ctx = read_signal_context() or {}
    return compute_temperature(
        ladder=ctx.get("lianban_ladder"),
        prev_ladder=ctx.get("prev_lianban_ladder"),
        limitup_total=ctx.get("limitup_total"),
        morning_quotes=morning_quotes,
    )


if __name__ == "__main__":
    import json
    print(json.dumps(read_temperature(), ensure_ascii=False, indent=2))
