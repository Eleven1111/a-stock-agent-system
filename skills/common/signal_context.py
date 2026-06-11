#!/usr/bin/env python3
"""
情绪上下文回流 — 板块赚钱效应/连板梯队/资金流喂给个股情绪面
==============================================================
本系统核心玩法是「抓赚钱效应板块 → 打板 + 高成长」，但 score_sentiment(25%) 此前
只看个股涨跌幅+换手率——最重要的维度却是最薄的。本模块把 hot-money（连板梯队、
板块涨停数、封板质量）与 capital_flow（北向、板块/个股主力资金）的产出落入共享缓存，
four_dim 情绪面直接消费。

写入方各管一块、合并落盘（mutate_json 单锁，互不覆盖）：
- capital_flow_monitor --cache → northbound / sector_flows / stock_flows
- hot-money analyze --cache    → sector_limitups / lianban_ladder / market_sentiment

缓存：$HERMES_HOME/skills/stock-triage/cache/signal_context.json
所有读取 fallback-safe：缓存缺失/过期时，情绪面行为与历史完全一致。
"""

import os
import sys
from datetime import datetime
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from paths import cache_dir  # noqa: E402
from state_store import mutate_json, read_json  # noqa: E402

DEFAULT_MAX_AGE_HOURS = 24  # 盘中/收盘写入，覆盖到次日


def context_file() -> str:
    return os.path.join(cache_dir("stock-triage"), "signal_context.json")


def update_signal_context(partial: Dict[str, Any]) -> Dict[str, Any]:
    """合并式写入：只更新提供的 key，保留其他写入方的数据（单锁事务）。

    梯队滚动：写入新交易日的 lianban_ladder（带 ladder_asof）时，把旧梯队滚动为
    prev_lianban_ladder——这是温度计"连板晋级率"的分母。同日重复写入只覆盖不滚动。
    """

    def _mut(data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            data = {}
        new_asof = partial.get("ladder_asof")
        if ("lianban_ladder" in partial and new_asof
                and data.get("lianban_ladder")
                and data.get("ladder_asof")
                and data["ladder_asof"] != new_asof):
            data["prev_lianban_ladder"] = data["lianban_ladder"]
            data["prev_ladder_asof"] = data["ladder_asof"]
        data.update(partial)
        data["schema"] = "signal_context_v1"
        data["generated_at"] = datetime.now().isoformat()
        return data

    return mutate_json(context_file(), _mut, {})


def read_signal_context(max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
                        now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """读情绪上下文；缺失或过期返回 None（情绪面回退历史逻辑）。"""
    record = read_json(context_file(), None)
    if not isinstance(record, dict) or not record.get("generated_at"):
        return None
    try:
        gen = datetime.fromisoformat(record["generated_at"])
    except (TypeError, ValueError):
        return None
    ref = now or datetime.now()
    if (ref - gen).total_seconds() > max_age_hours * 3600:
        return None
    return record


# ========== 纯函数：情绪加成（可单测）==========

def sentiment_boost(code: str, ctx: Optional[Dict[str, Any]],
                    sector: Optional[str] = None) -> Dict[str, Any]:
    """从上下文计算个股情绪加成。返回 {delta, notes, sector}；无上下文 → 0 加成。

    口径（打板/赚钱效应原生）：
    - 连板梯队在册：连板≥2 +1.5（梯队龙头延续性）；首板 +0.8
    - 封板资金≥1亿 +0.5；竞价/早盘封(≤09:35) +0.5（封板质量）
    - 板块涨停≥5 +1.0 / ≥3 +0.5（板块赚钱效应/集群共振）
    - 个股主力净流入>1亿 +0.5；净流出<-1亿 -0.5
    - 北向净流出<-30亿 -0.5（外资风险偏好收缩）
    """
    if not ctx:
        return {"delta": 0.0, "notes": [], "sector": sector}

    code = str(code).zfill(6)
    delta = 0.0
    notes = []

    ladder = (ctx.get("lianban_ladder") or {}).get(code)
    if isinstance(ladder, dict):
        sector = sector or ladder.get("sector")
        lianban = int(ladder.get("lianban") or 0)
        if lianban >= 2:
            delta += 1.5
            notes.append(f"{lianban}连板梯队在册")
        elif lianban == 1:
            delta += 0.8
            notes.append("首板在册")
        seal_yi = ladder.get("seal_yi")
        if isinstance(seal_yi, (int, float)) and seal_yi >= 1.0:
            delta += 0.5
            notes.append(f"封板资金{seal_yi:.1f}亿")
        first_seal = str(ladder.get("first_seal") or "")
        if first_seal and first_seal <= "09:35":
            delta += 0.5
            notes.append(f"早盘强封({first_seal})")

    limitups = ctx.get("sector_limitups") or {}
    if sector and sector in limitups:
        n = int(limitups[sector] or 0)
        if n >= 5:
            delta += 1.0
            notes.append(f"板块赚钱效应强({sector}涨停{n}家)")
        elif n >= 3:
            delta += 0.5
            notes.append(f"板块共振({sector}涨停{n}家)")

    flow = (ctx.get("stock_flows") or {}).get(code)
    if isinstance(flow, dict):
        main = flow.get("main_net_yi")
        if isinstance(main, (int, float)):
            if main > 1:
                delta += 0.5
                notes.append(f"主力净流入{main:.1f}亿")
            elif main < -1:
                delta -= 0.5
                notes.append(f"⚠️主力净流出{abs(main):.1f}亿")

    nb = ctx.get("northbound_net_yi")
    if isinstance(nb, (int, float)) and nb < -30:
        delta -= 0.5
        notes.append(f"⚠️北向净流出{abs(nb):.0f}亿")

    return {"delta": round(delta, 2), "notes": notes, "sector": sector}


if __name__ == "__main__":
    import json
    ctx = read_signal_context()
    print(json.dumps(ctx or {"found": False}, ensure_ascii=False, indent=2))
