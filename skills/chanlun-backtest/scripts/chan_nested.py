#!/usr/bin/env python3
"""
缠论区间套证据 — 日线/60分钟买卖点方向共振
============================================
对齐 docs/chanlun-upgrade-plan-2026-08.md T5：多级别父子对齐的最小实现。输入两个级别
（如日线 + 60 分钟）各自的 `chan_structure.analyze()['signals']` 列表，找出"日线近窗
确定买卖点方向 与 60m 近窗同向确定买卖点共现"的记录。

纪律红线：本模块只产出**展示用证据**，不打分、不预测方向、不参与 research_gate。
下游（four_dim_scorer）把输出记录渲染成 0 权重的 signals 备注，与既有
"[研究假设]…(未过闸·0权重)" 展示纪律一致。

纯函数边界：不修改任何入参（signals 列表原样只读）。
"""

from typing import Any, Dict, List, Sequence

DEFAULT_DAILY_WINDOW = 10     # 对齐 four_dim_scorer.score_technical 的 recent_window
DEFAULT_INTRADAY_WINDOW = 8   # 对齐 four_dim_scorer.score_short_term_entry 的 recent_window


def _recent_sure(signals: Sequence[Dict[str, Any]], total_bars: int, window: int) -> List[Dict[str, Any]]:
    """近窗（idx >= total_bars - window）且 is_sure=True 的信号，与
    four_dim_scorer.chan_adjustment 的"新鲜度"判据同口径。"""
    out = []
    for s in signals or []:
        idx = s.get("idx")
        if idx is None or idx < total_bars - window:
            continue
        if not s.get("is_sure"):
            continue
        out.append(s)
    return out


def find_nested_confirmations(
    daily_signals: Sequence[Dict[str, Any]],
    daily_total_bars: int,
    intraday_signals: Sequence[Dict[str, Any]],
    intraday_total_bars: int,
    daily_window: int = DEFAULT_DAILY_WINDOW,
    intraday_window: int = DEFAULT_INTRADAY_WINDOW,
) -> List[Dict[str, Any]]:
    """日线近窗确定买卖点 × 60m 近窗同向确定买卖点 → 区间套共振记录列表。

    每条记录含两级别的 bsp_type 与日期，仅供展示（0 权重）：
    {"direction": "buy"|"sell",
     "daily_bsp_type": ..., "daily_date": ..., "daily_idx": ...,
     "intraday_bsp_type": ..., "intraday_date": ..., "intraday_idx": ...}

    纯函数：不修改 daily_signals / intraday_signals。同向即成对（笛卡尔积），近窗
    默认较小（8~10 根），实际共现记录条数通常很少。
    """
    daily_recent = _recent_sure(daily_signals, daily_total_bars, daily_window)
    intraday_recent = _recent_sure(intraday_signals, intraday_total_bars, intraday_window)
    records: List[Dict[str, Any]] = []
    for d in daily_recent:
        for i in intraday_recent:
            if bool(d.get("is_buy")) != bool(i.get("is_buy")):
                continue
            records.append({
                "direction": "buy" if d.get("is_buy") else "sell",
                "daily_bsp_type": d.get("bsp_type"), "daily_date": d.get("date"),
                "daily_idx": d.get("idx"),
                "intraday_bsp_type": i.get("bsp_type"), "intraday_date": i.get("date"),
                "intraday_idx": i.get("idx"),
            })
    return records


def format_nested_notes(records: Sequence[Dict[str, Any]]) -> List[str]:
    """共振记录 → four_dim_scorer 的 signals 备注文本（0 权重，[研究假设] 展示纪律）。"""
    notes = []
    for r in records:
        side = "买" if r.get("direction") == "buy" else "卖"
        notes.append(
            f"[研究假设]区间套共振{side}:日线bsp{r.get('daily_bsp_type')}@{r.get('daily_date')}"
            f"×60m bsp{r.get('intraday_bsp_type')}@{r.get('intraday_date')}(0权重)"
        )
    return notes
