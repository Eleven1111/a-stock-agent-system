#!/usr/bin/env python3
"""
大盘上下文回流 — 让个股四维评分感知外围环境
============================================
断点修复：four_dim_scorer 此前完全不读 global-market-monitor，VIX 飙升/纳指暴跌时
个股评分毫无感知。本模块把 global_monitor 的 assess_impact 产出落入共享缓存，
four_dim 在出分后叠加一层"大盘 overlay"：大盘系统性承压时给个股评分降档/封顶，
顺风时只标注不追高。

设计取舍：overlay 不改动四维内部分值（保持各维度纯净），只在最终 grade/advice 上做
封顶式调整。缺失、过期或异常上下文是一等状态，必须阻断方向性新风险，不能解释为中性。

缓存：$HERMES_HOME/skills/stock-triage/cache/market_context.json
数据源：本地 JSON，cron-safe。
"""

import os
import sys
from datetime import datetime
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from paths import cache_dir  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402

GRADE_ORDER = ["S", "A", "B", "C", "D"]
DEFAULT_MAX_AGE_HOURS = 18      # 盘前/隔夜写入，覆盖到次日开盘前
RISK_OFF_THRESHOLD = -6         # 板块影响合计 ≤ 此值 → 系统性承压
RISK_ON_THRESHOLD = 6


def context_file() -> str:
    return os.path.join(cache_dir("stock-triage"), "market_context.json")


# ========== 读写 ==========

def write_market_context(impact: Dict[str, Any], asof: Optional[str] = None) -> Dict[str, Any]:
    """把 assess_impact 产出 {alerts, sector_impact, summary} 落入共享缓存。"""
    record = {
        "schema": "market_context_v1",
        "asof": asof or datetime.now().date().isoformat(),
        "generated_at": datetime.now().isoformat(),
        "alerts": impact.get("alerts", []),
        "sector_impact": impact.get("sector_impact", {}),
        "summary": impact.get("summary", ""),
        "status": impact.get("status", "ok"),
    }
    atomic_write_json(context_file(), record)
    return record


def _unavailable_context(
    status: str,
    reason: str,
    record: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    out = dict(record or {})
    out.update({
        "schema": out.get("schema") or "market_context_v1",
        "context_status": status,
        "context_fresh": False,
        "unavailable_reason": reason,
    })
    return out


def read_market_context(max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
                        now: Optional[datetime] = None) -> Dict[str, Any]:
    """读大盘上下文；缺失、异常和过期都返回可审计的非中性状态。"""
    record = read_json(context_file(), None)
    if not isinstance(record, dict):
        return _unavailable_context("unknown", "大盘上下文缺失或无法读取")
    if not record.get("generated_at"):
        return _unavailable_context("unknown", "大盘上下文缺少 generated_at", record)
    try:
        gen = datetime.fromisoformat(record["generated_at"])
    except (TypeError, ValueError):
        return _unavailable_context("unknown", "大盘上下文 generated_at 无效", record)
    ref = now or datetime.now()
    if (ref - gen).total_seconds() > max_age_hours * 3600:
        return _unavailable_context("stale", "大盘上下文已过期", record)
    out = dict(record)
    out.update({"context_status": "fresh", "context_fresh": True})
    return out


# ========== 纯函数：态势判定 + overlay ==========

def market_regime(ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从大盘上下文判定风险态势（纯函数）。"""
    if not ctx:
        return {"regime": "unknown", "reason": "无大盘数据", "score": None}
    context_status = str(ctx.get("context_status") or "")
    if context_status in {"unknown", "stale"}:
        return {
            "regime": context_status,
            "reason": str(ctx.get("unavailable_reason") or "大盘上下文不可用"),
            "score": None,
        }
    upstream_status = str(ctx.get("status") or "").lower()
    if upstream_status and upstream_status not in {"ok", "ready"}:
        return {
            "regime": "unknown",
            "reason": f"大盘上下文上游状态异常: {upstream_status}",
            "score": None,
        }
    sector_impact = ctx.get("sector_impact", {}) or {}
    alerts = ctx.get("alerts", []) or []
    score = sum(v for v in sector_impact.values() if isinstance(v, (int, float)))
    red = [a for a in alerts if "🔴" in str(a.get("level", ""))]
    market_wide_red = any("全市场" in (a.get("sectors") or []) for a in red)

    if score <= RISK_OFF_THRESHOLD or (market_wide_red and score < 0):
        return {"regime": "risk_off",
                "reason": f"板块影响合计{score:+d}，{len(red)}条高级别预警",
                "score": score}
    if score >= RISK_ON_THRESHOLD:
        return {"regime": "risk_on", "reason": f"板块影响合计{score:+d}", "score": score}
    return {"regime": "neutral", "reason": f"板块影响合计{score:+d}", "score": score}


def downgrade(grade: str, steps: int = 1) -> str:
    if grade not in GRADE_ORDER:
        return grade
    return GRADE_ORDER[min(len(GRADE_ORDER) - 1, GRADE_ORDER.index(grade) + steps)]


def apply_market_overlay(result: Dict[str, Any], ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """在四维评分结果上叠加大盘 overlay（返回新 dict，不 mutate 入参）。"""
    regime = market_regime(ctx)
    r = regime["regime"]
    out = dict(result)
    if r in {"unknown", "stale"}:
        old = out.get("grade")
        out["grade"] = "D"
        out["advice"] = f"⚠️大盘上下文{r}（{regime['reason']}），仅供研究｜{out.get('advice', '')}"
        out["market_overlay"] = {
            "regime": r,
            "reason": regime["reason"],
            "grade_from": old,
            "grade_to": "D",
        }
    elif r == "risk_off":
        old = out.get("grade")
        new = downgrade(old, 1)
        out["grade"] = new
        out["advice"] = f"⚠️大盘承压（{regime['reason']}）｜{out.get('advice', '')}"
        out["market_overlay"] = {"regime": r, "reason": regime["reason"],
                                 "grade_from": old, "grade_to": new}
    elif r == "risk_on":
        out["advice"] = f"{out.get('advice', '')}（大盘顺风，勿追高）"
        out["market_overlay"] = {"regime": r, "reason": regime["reason"]}
    else:
        out["market_overlay"] = {"regime": "neutral", "reason": regime["reason"]}
    return out
