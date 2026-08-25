#!/usr/bin/env python3
"""
分钟线派生字段 — 纯函数层（不触网、可离线单测）
================================================
事件表 v4 里 S1/S2 两个策略唯一还缺的证据字段，都只能从**分钟级**量价派生：

- ``volume_ratio``            S1 条件3：09:45 前量比 = 截至该时刻每分钟均量 ÷ 基准每分钟均量。
- ``pre_reseal_turnover_pct`` S2 条件4：封板（回封）前累计换手率(%)。

本模块只做「分钟行 → 派生值」这一步，铁律三条：

1. **禁未来函数**：两个派生函数都只吃 ``until_time`` 之前（含该时刻收线的那根）的行。
   多喂后续行不得改变结果 —— 见 tests/test_minute_derived.py 的截断/全天对照用例。
2. **单位与累计语义必须显式换算**，不许猜：
   - 腾讯分时 ``cum_volume`` 是**累计**成交量、单位「手」（1 手 = 100 股，实测
     cum_amount / (cum_volume × 100) = 成交均价）；
   - 新浪分钟 K ``volume`` 是**增量**成交量、单位「股」（实测 amount / volume = 均价）。
   两条来源在 2026-08-25 sz000001 上交叉验证：截至 09:45 腾讯 146,525 手 vs 新浪
   14,668,889 股 = 146,689 手，差 0.11%（边界归属差异）。仓内出过「volume×close 漏乘
   每手股数把成交额低估 100 倍」的事故，所以换算一律走本模块，不在调用点手写。
3. **fail-closed**：行缺失 / 覆盖不到 checkpoint / 行数不足以覆盖该时段 / 缺流通股本，
   一律返回 ``value=None`` + ``unavailable:<原因>``，**绝不返回 0，绝不用日线代理值**。

流通股本口径沿用事件表 v4：``float_shares = 流通市值 ÷ 事件日收盘价``（daban_bt_data.
turnover_baseline 同源），已知偏差同样继承 —— 流通市值取自涨停池快照，与收盘价可能不
同步，个位数百分比的误差属已知，不做二次修正。
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional, Sequence

AVAILABLE = "available"
UNAVAILABLE = "unavailable"

# 沪深 A 股每手股数。与 execution_constraints.volume_lot_shares 同口径，
# 这里保留常量是为了让本模块可以脱离配置单测（调用方可显式传入覆盖）。
LOT_SHARES = 100.0

# 连续竞价时段边界（分钟数 = HH*60+MM）。
_OPEN = 9 * 60 + 30      # 570
_MORNING_CLOSE = 11 * 60 + 30   # 690
_AFTERNOON_OPEN = 13 * 60       # 780
_CLOSE = 15 * 60                # 900
SESSION_MINUTES = 240

# 来源标签 —— 写进事件表的 ``volume_ratio_source``，S1 用它判 degraded。
SOURCE_TENCENT_INTRADAY = "tencent_minute_intraday"
SOURCE_SINA_5MIN = "sina_5min_history"


# --------------------------------------------------------------------------- #
# 时间与工具
# --------------------------------------------------------------------------- #
def parse_minute(value: Any) -> Optional[int]:
    """'0945' / '09:45' / '09:45:00' / '2026-08-25 09:45:00' → 585；非法 → None（不猜）。"""
    text = str(value or "").strip()
    if not text:
        return None
    if " " in text:
        text = text.split(" ", 1)[1]
    text = text.replace(":", "")
    if len(text) < 4 or not text[:4].isdigit():
        return None
    hour, minute = int(text[:2]), int(text[2:4])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def elapsed_trading_minutes(minute: int) -> int:
    """09:30 到 ``minute`` 之间已走过的连续竞价分钟数（跳过午休），钳制在 [0, 240]。"""
    if minute <= _OPEN:
        return 0
    if minute <= _MORNING_CLOSE:
        return minute - _OPEN
    if minute < _AFTERNOON_OPEN:
        return _MORNING_CLOSE - _OPEN
    return min(SESSION_MINUTES, (_MORNING_CLOSE - _OPEN) + (minute - _AFTERNOON_OPEN))


def _positive(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _non_negative(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


# --------------------------------------------------------------------------- #
# 归一化：两条来源 → 统一的「增量成交量（股）」分钟行
# --------------------------------------------------------------------------- #
def normalize_tencent_minute(rows: Optional[Iterable[Mapping[str, Any]]],
                             lot_shares: float = LOT_SHARES
                             ) -> Optional[list[dict[str, Any]]]:
    """腾讯分时（累计量，手）→ 规范行 [{minute, time, volume_shares, amount}]（增量，股）。

    累计序列非单调（回退）说明这份数据本身坏了 → 返回 None 让调用方 fail-closed，
    **不 clamp 成 0**：把坏数据修成看起来正常的样子，正是「假绿」的温床。
    """
    lot = _positive(lot_shares)
    if lot is None:
        return None
    out: list[dict[str, Any]] = []
    prev_volume = 0.0
    prev_amount = 0.0
    for row in rows or []:
        minute = parse_minute(row.get("time"))
        cum_volume = _non_negative(row.get("cum_volume"))
        cum_amount = _non_negative(row.get("cum_amount"))
        if minute is None or cum_volume is None or cum_amount is None:
            return None
        if cum_volume < prev_volume or cum_amount < prev_amount:
            return None
        out.append({
            "minute": minute,
            "time": f"{minute // 60:02d}:{minute % 60:02d}",
            "volume_shares": (cum_volume - prev_volume) * lot,
            "amount": cum_amount - prev_amount,
        })
        prev_volume, prev_amount = cum_volume, cum_amount
    return out


def normalize_sina_minute(rows: Optional[Iterable[Mapping[str, Any]]]
                          ) -> Optional[list[dict[str, Any]]]:
    """新浪分钟 K（增量量，股，bar 以**收线时刻**标注）→ 规范行。字段坏一行即整份作废。"""
    out: list[dict[str, Any]] = []
    for row in rows or []:
        minute = parse_minute(row.get("day") or row.get("time"))
        volume = _non_negative(row.get("volume"))
        amount = _non_negative(row.get("amount"))
        if minute is None or volume is None:
            return None
        out.append({
            "minute": minute,
            "time": f"{minute // 60:02d}:{minute % 60:02d}",
            "volume_shares": volume,
            "amount": amount if amount is not None else 0.0,
        })
    return out


def _cadence(rows: Sequence[Mapping[str, Any]]) -> Optional[int]:
    """相邻行的最小正间隔 = 这份数据的分钟粒度（1 / 5 / 15…）。单行无法判定 → None。"""
    gaps = [
        int(rows[i]["minute"]) - int(rows[i - 1]["minute"])
        for i in range(1, len(rows))
        if int(rows[i]["minute"]) - int(rows[i - 1]["minute"]) > 0
    ]
    return min(gaps) if gaps else None


def _window(rows: Optional[Sequence[Mapping[str, Any]]], until_minute: int
            ) -> tuple[Optional[list[Mapping[str, Any]]], Optional[str]]:
    """截取 minute <= until_minute 的行，并核对这段时间**真的被数据覆盖**。

    覆盖判据两条，都是为了不把「只抓到前 3 分钟」当成「这段时间就只成交了这么多」：
      - 必须有行落在 until_minute 当刻或之后（否则数据被截断在 checkpoint 之前）；
      - 窗口内行数 >= 该时段应有的根数（elapsed // cadence），否则中间有洞。
    """
    if not rows:
        return None, "minute_rows_missing"
    ordered = sorted(rows, key=lambda row: int(row["minute"]))
    if int(ordered[-1]["minute"]) < until_minute:
        return None, "minute_rows_truncated_before_checkpoint"
    window = [row for row in ordered if int(row["minute"]) <= until_minute]
    if not window:
        return None, "minute_rows_start_after_checkpoint"
    cadence = _cadence(ordered)
    if cadence is None:
        return None, "minute_rows_single_bar"
    expected = elapsed_trading_minutes(until_minute) // cadence
    if len(window) < expected:
        return None, f"minute_rows_incomplete({len(window)}<{expected})"
    return window, None


def downsample_rows(rows: Optional[Sequence[Mapping[str, Any]]],
                    step_minutes: int = 5) -> Optional[list[dict[str, Any]]]:
    """规范行 → 每 step 分钟一根（增量相加），bar 以**收线时刻**标注。

    这是落盘用的压缩形式：全天 48 个数就能复原任意时刻的累计量，而原始分钟条
    5000 股 × 240 根太重。归桶规则与新浪 5 分钟线对齐 —— 09:30 的集合竞价成交
    并入 09:35 那根，午休时段不产生桶。
    """
    step = int(step_minutes)
    if step <= 0 or rows is None:
        return None
    buckets: dict[int, dict[str, float]] = {}
    for row in rows:
        minute = int(row["minute"])
        offset = max(elapsed_trading_minutes(minute), 1)
        slot_offset = ((offset + step - 1) // step) * step
        slot = _OPEN + slot_offset
        if slot > _MORNING_CLOSE:
            slot = _AFTERNOON_OPEN + (slot - _MORNING_CLOSE)
        bucket = buckets.setdefault(slot, {"volume_shares": 0.0, "amount": 0.0})
        bucket["volume_shares"] += float(row.get("volume_shares", 0.0) or 0.0)
        bucket["amount"] += float(row.get("amount", 0.0) or 0.0)
    return [
        {"minute": slot, "time": f"{slot // 60:02d}:{slot % 60:02d}",
         "volume_shares": values["volume_shares"], "amount": values["amount"]}
        for slot, values in sorted(buckets.items())
    ]


def rows_to_slots(rows: Optional[Sequence[Mapping[str, Any]]]) -> dict[str, float]:
    """规范行 → {'HHMM': 增量成交股数}，落盘用（JSON 友好、无嵌套）。"""
    return {
        f"{int(row['minute']) // 60:02d}{int(row['minute']) % 60:02d}":
            round(float(row.get("volume_shares", 0.0) or 0.0), 2)
        for row in rows or []
    }


def slots_to_rows(slots: Optional[Mapping[str, Any]]) -> Optional[list[dict[str, Any]]]:
    """落盘的 slots → 规范行；键非法即整份作废（半份数据比没数据更危险）。"""
    if not slots:
        return None
    out: list[dict[str, Any]] = []
    for key, value in slots.items():
        minute = parse_minute(key)
        volume = _non_negative(value)
        if minute is None or volume is None:
            return None
        out.append({"minute": minute, "time": f"{minute // 60:02d}:{minute % 60:02d}",
                    "volume_shares": volume, "amount": 0.0})
    return sorted(out, key=lambda row: row["minute"])


# --------------------------------------------------------------------------- #
# 派生字段
# --------------------------------------------------------------------------- #
def volume_ratio_at(minute_rows: Optional[Sequence[Mapping[str, Any]]],
                    checkpoint: str = "09:45",
                    baseline_per_minute: Any = None) -> dict[str, Any]:
    """量比 = 截至 checkpoint 的每分钟均量 ÷ 基准每分钟均量。

    分母是**已走过的连续竞价分钟数**（elapsed_trading_minutes），不是行数 —— 5 分钟
    线到 09:45 只有 3 根，但已经走了 15 分钟，用行数会把量比放大 5 倍。
    集合竞价成交量含在 09:30/09:35 那根里，属于分子（与市场通行的量比口径一致）。
    """
    until = parse_minute(checkpoint)
    if until is None:
        return {"value": None, "availability": f"{UNAVAILABLE}:bad_checkpoint({checkpoint})"}
    baseline = _positive(baseline_per_minute)
    if baseline is None:
        return {"value": None, "availability": f"{UNAVAILABLE}:baseline_per_minute_unavailable"}
    window, reason = _window(minute_rows, until)
    if window is None:
        return {"value": None, "availability": f"{UNAVAILABLE}:{reason}"}
    elapsed = elapsed_trading_minutes(until)
    if elapsed <= 0:
        return {"value": None, "availability": f"{UNAVAILABLE}:checkpoint_before_open({checkpoint})"}
    traded = sum(float(row["volume_shares"]) for row in window)
    return {
        "value": round(traded / elapsed / baseline, 6),
        "availability": AVAILABLE,
        "checkpoint": f"{until // 60:02d}:{until % 60:02d}",
        "elapsed_minutes": elapsed,
        "traded_shares": traded,
    }


def cumulative_turnover_before(minute_rows: Optional[Sequence[Mapping[str, Any]]],
                               until_time: Any,
                               float_shares: Any) -> dict[str, Any]:
    """封板前累计换手率(%) = 截至 until_time 的累计成交股数 ÷ 流通股本 × 100。

    含「收线时刻恰为 until_time」的那一根：该 bar 里的成交全部发生在 until_time 之前
    （新浪 bar 以收线时刻标注），排除它会系统性低估。
    """
    until = parse_minute(until_time)
    if until is None:
        return {"value": None, "availability": f"{UNAVAILABLE}:until_time_missing"}
    shares = _positive(float_shares)
    if shares is None:
        return {"value": None, "availability": f"{UNAVAILABLE}:float_shares_unavailable"}
    window, reason = _window(minute_rows, until)
    if window is None:
        return {"value": None, "availability": f"{UNAVAILABLE}:{reason}"}
    traded = sum(float(row["volume_shares"]) for row in window)
    return {
        "value": round(traded / shares * 100.0, 6),
        "availability": AVAILABLE,
        "until": f"{until // 60:02d}:{until % 60:02d}",
        "traded_shares": traded,
    }


def float_shares_from_mktcap(float_mktcap: Any, ref_close: Any) -> Optional[float]:
    """流通股本 = 流通市值 ÷ 收盘价（v4 口径，偏差说明见模块 docstring）。"""
    cap = _positive(float_mktcap)
    close = _positive(ref_close)
    if cap is None or close is None:
        return None
    return cap / close


def baseline_per_minute_from_daily(kline: Optional[Sequence[Mapping[str, Any]]],
                                   date: str,
                                   window_days: int = 5,
                                   lot_shares: float = LOT_SHARES) -> dict[str, Any]:
    """量比基准：事件日**之前** N 个交易日的每分钟均量（股/分钟）。

    口径写死在这里而不是调用点：过去 N 日总成交股数 ÷ (N × 240)。日线 volume 单位是
    「手」，这里乘 lot_shares 折成股，与分子的股口径对齐。样本不足 N 天 → unavailable
    （绝不用手上有的几天凑一个看起来正常的数）。
    """
    target = str(date or "").strip()
    rows = list(kline or [])
    index = next((i for i, bar in enumerate(rows) if str(bar.get("date")) == target), None)
    if index is None:
        return {"value": None, "availability": f"{UNAVAILABLE}:event_date_not_in_kline"}
    lot = _positive(lot_shares)
    if lot is None:
        return {"value": None, "availability": f"{UNAVAILABLE}:bad_lot_shares"}
    samples = [
        _positive(bar.get("volume"))
        for bar in rows[max(0, index - int(window_days)):index]
    ]
    usable = [value for value in samples if value is not None]
    if len(usable) < int(window_days):
        return {"value": None, "sample_days": len(usable),
                "availability": (f"{UNAVAILABLE}:baseline_sample_insufficient"
                                 f"({len(usable)}<{int(window_days)})")}
    total_shares = sum(usable) * lot
    return {"value": total_shares / (len(usable) * SESSION_MINUTES),
            "sample_days": len(usable), "availability": AVAILABLE}


def derive_minute_fields(minute_rows: Optional[Sequence[Mapping[str, Any]]],
                         *,
                         source: str,
                         checkpoint: str = "09:45",
                         baseline_per_minute: Any = None,
                         reseal_time: Any = None,
                         float_shares: Any = None
                         ) -> dict[str, Any]:
    """一次算齐 S1/S2 两个字段 + 逐字段可得性 + 来源标签（写进事件表用）。

    ``reseal_time`` 为 None 表示这只票当天没炸过板 —— 那就**不存在**封板前换手这个量，
    照 v4 的三态口径由调用方保留 not_applicable，本函数只负责报 unavailable 原因。
    """
    ratio = volume_ratio_at(minute_rows, checkpoint=checkpoint,
                            baseline_per_minute=baseline_per_minute)
    turnover = cumulative_turnover_before(minute_rows, reseal_time, float_shares)
    return {
        "volume_ratio": ratio["value"],
        "volume_ratio_availability": ratio["availability"],
        "volume_ratio_source": f"{source}:{checkpoint}" if ratio["value"] is not None else None,
        "pre_reseal_turnover_pct": turnover["value"],
        "pre_reseal_turnover_pct_availability": turnover["availability"],
        "pre_reseal_turnover_source": f"{source}:{turnover.get('until')}"
        if turnover["value"] is not None else None,
    }


__all__ = [
    "AVAILABLE", "UNAVAILABLE", "LOT_SHARES", "SESSION_MINUTES",
    "SOURCE_TENCENT_INTRADAY", "SOURCE_SINA_5MIN",
    "parse_minute", "elapsed_trading_minutes",
    "normalize_tencent_minute", "normalize_sina_minute",
    "downsample_rows", "rows_to_slots", "slots_to_rows",
    "volume_ratio_at", "cumulative_turnover_before",
    "float_shares_from_mktcap", "baseline_per_minute_from_daily",
    "derive_minute_fields",
]
