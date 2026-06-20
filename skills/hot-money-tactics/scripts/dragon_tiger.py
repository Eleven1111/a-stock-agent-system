#!/usr/bin/env python3
"""
游资战法 — 龙虎榜席位数据源（席位分类 / 游资识别 / 净买汇总）
================================================================
补齐 data_sources.md 长期标注的「龙虎榜席位数据未实现」缺口。akshare 东财龙虎榜
接口本机 TUN 下实测可用：stock_lhb_detail_em(明细) + stock_lhb_stock_detail_em(买卖
前5营业部席位) + stock_lhb_hyyyb_em(活跃营业部)。

席位分类把"谁在买"拆成三类——机构(institution)/北向(northbound)/游资营业部
(hot_money)，游资净买主导=打板资金特征，机构净买主导=价值资金特征，是游资战法的
关键信号维度。知名游资席位(宁波解放南路/拉萨天团/章盟主系等)额外打 famous 标。

价值边界（诚实）：
- ✅ 席位营业部名称、买卖金额、净额：免费东财源完整提供。
- ⚠️ 游资识别基于营业部名称关键词匹配（HOT_MONEY_SEATS 可扩展），非官方资金归属；
     "机构专用"不披露具体机构，北向仅深/沪股通汇总。famous 标记是辅助线索非硬信号。
- ❌ 接入打分前必须过 research_gate 研究闸门（与本仓库 mootdx 深历史源同一红线）。

纯函数（classify_seat / is_famous_seat / summarize_seats / seat_signal）可合成数据
单测；触网函数（fetch_lhb_detail / fetch_seats / build_seat_signal）手动冒烟。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

INSTITUTION_MARKERS = ("机构专用",)
NORTHBOUND_MARKERS = ("深股通专用", "沪股通专用")

# 业界公认知名游资/打板席位关键词（地名或营业部特征），可扩展。非官方名单，仅作线索。
HOT_MONEY_SEATS = (
    "宁波解放南路", "宁波桑田路", "宁波中山东路",     # 宁波系（涨停敢死队发源）
    "拉萨", "西藏东方财富", "西藏同信",               # 拉萨天团（东财散户+游资聚集）
    "上海溧阳路", "上海江苏路", "上海普陀区江宁路",     # 上海系
    "南京太平南路", "成都南一环路", "佛山顺德大良",
    "深圳益田路", "深圳福华一路", "杭州体育场路", "绍兴",
)


def classify_seat(name: str) -> str:
    """营业部名称 → 席位类型：institution / northbound / hot_money。"""
    name = str(name)
    if any(m in name for m in INSTITUTION_MARKERS):
        return "institution"
    if any(m in name for m in NORTHBOUND_MARKERS):
        return "northbound"
    return "hot_money"   # 具名营业部席位 = 游资资金代理


def is_famous_seat(name: str) -> bool:
    """是否知名游资/打板席位（关键词匹配，辅助线索）。"""
    name = str(name)
    return any(kw in name for kw in HOT_MONEY_SEATS)


def _net(row: Dict[str, Any]) -> float:
    """席位净额：优先用「净额」字段，缺失则买入金额-卖出金额。"""
    if row.get("净额") is not None:
        try:
            return float(row["净额"])
        except (TypeError, ValueError):
            pass
    buy = float(row.get("买入金额", 0) or 0)
    sell = float(row.get("卖出金额", 0) or 0)
    return buy - sell


def summarize_seats(seats: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    买卖席位合并 → 按类型汇总净买额 + 知名游资名单。
    seats 每行需含「交易营业部名称」与「净额」(或买入/卖出金额)。
    """
    agg = {"institution": 0.0, "northbound": 0.0, "hot_money": 0.0}
    famous: List[str] = []
    seen = set()
    for row in seats:
        name = str(row.get("交易营业部名称", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        kind = classify_seat(name)
        agg[kind] += _net(row)
        if kind == "hot_money" and is_famous_seat(name):
            famous.append(name)
    return {
        "institution_net": round(agg["institution"], 2),
        "northbound_net": round(agg["northbound"], 2),
        "hot_money_net": round(agg["hot_money"], 2),
        "famous_seats": famous,
        "seat_count": len(seen),
    }


def seat_signal(summary: Dict[str, Any]) -> Dict[str, Any]:
    """席位汇总 → 资金性质裁决：谁主导、是否有知名游资、净买方向。"""
    nets = {
        "hot_money": summary.get("hot_money_net", 0.0),
        "institution": summary.get("institution_net", 0.0),
        "northbound": summary.get("northbound_net", 0.0),
    }
    dominant = max(nets, key=lambda k: abs(nets[k])) if any(nets.values()) else "none"
    return {
        "dominant_force": dominant,
        "hot_money_led": dominant == "hot_money" and nets["hot_money"] > 0,
        "has_famous_hot_money": bool(summary.get("famous_seats")),
        "total_net": round(sum(nets.values()), 2),
        **summary,
    }


# --------------------------------------------------------------------------- #
# 触网（手动冒烟）
# --------------------------------------------------------------------------- #
def fetch_lhb_detail(start: str, end: str):
    """龙虎榜明细 DataFrame。start/end 形如 '20260616'。
    非交易日/空区间返回 None（akshare 对空响应会抛 TypeError，此处兜住）。"""
    import akshare as ak
    try:
        return ak.stock_lhb_detail_em(start_date=start, end_date=end)
    except Exception:   # noqa: BLE001 — 非交易日 akshare 内部 NoneType 下标
        return None


def fetch_seats(code: str, date: str, flag: str = "买入") -> List[Dict[str, Any]]:
    """个股某日买卖前5营业部席位（flag='买入'/'卖出'）→ list[dict]。失败返回空。"""
    import akshare as ak
    try:
        df = ak.stock_lhb_stock_detail_em(symbol=str(code).zfill(6), date=date, flag=flag)
    except Exception:   # noqa: BLE001 — 个别(票,日)无榜单数据
        return []
    return df.to_dict("records") if df is not None and len(df) else []


def build_seat_signal(code: str, date: str) -> Dict[str, Any]:
    """端到端：某票某上榜日 → 买卖席位合并 → 资金性质信号。date 形如 '20260617'。"""
    seats = fetch_seats(code, date, "买入") + fetch_seats(code, date, "卖出")
    signal = seat_signal(summarize_seats(seats))
    signal["code"] = str(code).zfill(6)
    signal["date"] = date
    return signal
