#!/usr/bin/env python3
"""
打板回测 — 数据层（历史涨停事件 + 次日 K 线 → 事件表）
========================================================
逐日拉 akshare.stock_zt_pool_em（不走 push2，本机 TUN 下可用）取历史涨停事件，
收集代码后批量拉腾讯 ifzq 日线，把 T 收 / T+1 开 / T+1 收 join 进事件表。

为保证 gap 口径一致，t_close / t1_open / t1_close 统一取自同一份（qfq）K 线，
zt_pool 只负责事件筛选与 first_seal/连板/封单等元数据。

纯函数（kline_lookup / assemble_events）可用合成数据单测；
触网函数（fetch_limitup_events / fetch_klines）手动冒烟。
"""

import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))
from a_stock_http import fetch_tencent_kline, DataSourceError  # noqa: E402
from state_store import read_json, atomic_write_json  # noqa: E402
from paths import data_file  # noqa: E402


def market_prefix(code: str) -> str:
    """主板代码 → 腾讯市场前缀。60→sh，00→sz。"""
    code = str(code).zfill(6)
    return "sh" if code.startswith("6") else "sz"


def _norm_date(value: Any) -> str:
    """'20260603' / '2026-06-03' → '2026-06-03'。"""
    text = str(value).strip().replace("-", "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return str(value)


def kline_lookup(kline: List[Dict[str, Any]], date: str
                 ) -> Optional[Tuple[float, float, float]]:
    """
    在一只票的日线序列里定位 date，返回 (t_close, t1_open, t1_close)。
    date 不在序列、或没有次日（最后一根）→ None（事件丢弃）。
    """
    target = _norm_date(date)
    for i, bar in enumerate(kline):
        if _norm_date(bar.get("date")) == target:
            if i + 1 >= len(kline):
                return None
            nxt = kline[i + 1]
            return float(bar["close"]), float(nxt["open"]), float(nxt["close"])
    return None


def assemble_events(raw_events: List[Dict[str, Any]],
                    kline_by_code: Dict[str, List[Dict[str, Any]]]
                    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    把原始涨停事件 + 各代码 K 线 join 成回测事件表。
    返回 (事件表, 丢弃统计{no_kline, no_next_day})。
    """
    out: List[Dict[str, Any]] = []
    dropped = {"no_kline": 0, "no_next_day": 0}
    for ev in raw_events:
        code = str(ev.get("code", "")).zfill(6)
        kline = kline_by_code.get(code)
        if not kline:
            dropped["no_kline"] += 1
            continue
        looked = kline_lookup(kline, ev.get("date"))
        if looked is None:
            dropped["no_next_day"] += 1
            continue
        t_close, t1_open, t1_close = looked
        out.append({
            "code": code,
            "name": ev.get("name", code),
            "date": _norm_date(ev.get("date")),
            "t_close": t_close,
            "t1_open": t1_open,
            "t1_close": t1_close,
            "first_seal": ev.get("first_seal"),
            "lianban": ev.get("lianban"),
            "seal_amount": ev.get("seal_amount"),
            "float_mktcap": ev.get("float_mktcap"),
            "sector": ev.get("sector"),
            "is_st": bool(ev.get("is_st", False)),
        })
    return out, dropped


def _map_zt_row(row: Dict[str, Any], date: str) -> Dict[str, Any]:
    """stock_zt_pool_em 单行 → 标准化原始事件（仅元数据，价格留给 K 线）。"""
    name = str(row.get("名称", ""))
    return {
        "code": str(row.get("代码", "")).zfill(6),
        "name": name,
        "date": _norm_date(date),
        "first_seal": row.get("首次封板时间"),
        "lianban": row.get("连板数"),
        "seal_amount": row.get("封板资金"),
        "float_mktcap": row.get("流通市值"),
        "sector": row.get("所属行业"),
        "is_st": "ST" in name.upper(),
    }


def fetch_limitup_events(start: str, end: str, sleep: float = 0.3) -> List[Dict[str, Any]]:
    """逐交易日拉历史涨停池。start/end 形如 '20260301'。非交易日 zt_pool 空，自动跳过。"""
    import akshare as ak
    import pandas as pd

    raw: List[Dict[str, Any]] = []
    for day in pd.date_range(_norm_date(start), _norm_date(end), freq="D"):
        ymd = day.strftime("%Y%m%d")
        try:
            df = ak.stock_zt_pool_em(date=ymd)
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        for _, row in df.iterrows():
            raw.append(_map_zt_row(row.to_dict(), ymd))
        time.sleep(sleep)
    return raw


def fetch_klines(codes: List[str], days: int = 180, sleep: float = 0.2
                 ) -> Dict[str, List[Dict[str, Any]]]:
    """批量拉各代码腾讯 qfq 日线。失败的代码跳过（事件随后按 no_kline 丢弃）。"""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for code in sorted(set(str(c).zfill(6) for c in codes)):
        try:
            kl = fetch_tencent_kline(code, market=market_prefix(code), days=days)
        except DataSourceError:
            kl = []
        if kl:
            out[code] = kl
        time.sleep(sleep)
    return out


def control_pools_from_klines(kline_by_code: Dict[str, List[Dict[str, Any]]],
                              n_random: int = 300, breakout_window: int = 20,
                              seed: int = 42) -> Dict[str, List[float]]:
    """
    用已抓的 universe 日线算两个对照组的次日净收益池（不额外触网）：
    - simple_breakout：close 突破前 N 日新高 → 买次日开、卖次日收。
    - random_entry：随机 (代码, 交易日) → 买次日开、卖次日收。
    注意：池子取自涨停票历史，带轻微热门偏差，仅作 MVP 弱基准，正式跑应换全市场样本。
    """
    sys.path.insert(0, os.path.dirname(__file__))
    from daban_bt_engine import net_return  # noqa: E402
    import random as _random

    breakout: List[float] = []
    pairs: List[Tuple[str, int]] = []
    for code, kline in kline_by_code.items():
        closes = [float(b["close"]) for b in kline]
        for i in range(len(kline) - 1):
            pairs.append((code, i))
            if i >= breakout_window and closes[i] > max(closes[i - breakout_window:i]):
                try:
                    breakout.append(net_return(kline[i + 1]["open"], kline[i + 1]["close"]))
                except (ValueError, KeyError, TypeError):
                    continue

    rng = _random.Random(seed)
    rand: List[float] = []
    for code, i in rng.sample(pairs, min(n_random, len(pairs))) if pairs else []:
        nxt = kline_by_code[code][i + 1]
        try:
            rand.append(net_return(nxt["open"], nxt["close"]))
        except (ValueError, KeyError, TypeError):
            continue
    return {"simple_breakout": breakout, "random_entry": rand}


def _auto_days(start: str, buffer: int = 30) -> int:
    """腾讯 K 线按「最近 N 根」返回，N 必须覆盖从 start 回溯到今天的交易日数。"""
    from datetime import date

    y, m, d = (int(x) for x in _norm_date(start).split("-"))
    span = (date.today() - date(y, m, d)).days
    return int(span * 5 / 7) + buffer   # 日历天→交易日近似 + buffer


def _expected_trading_days(start: str, end: str) -> int:
    """请求区间内的近似交易日数（日历天 × 5/7），用于核对实际覆盖度。"""
    from datetime import date

    ys, ms, ds = (int(x) for x in _norm_date(start).split("-"))
    ye, me, de = (int(x) for x in _norm_date(end).split("-"))
    span = (date(ye, me, de) - date(ys, ms, ds)).days
    return max(1, int(span * 5 / 7))


def assess_coverage(raw: List[Dict[str, Any]], start: str, end: str) -> Dict[str, Any]:
    """核对涨停事件实际覆盖的交易日 vs 请求区间，缺失过半即高声告警。"""
    dates = sorted({_norm_date(e.get("date")) for e in raw})
    expected = _expected_trading_days(start, end)
    covered = len(dates)
    ratio = covered / expected if expected else 0.0
    warning = None
    if ratio < 0.8:
        warning = (f"⚠️ 数据覆盖严重不足：请求约 {expected} 个交易日，实际只拿到 {covered} 天"
                   f"（{_norm_date(start)}~{_norm_date(end)} 实际覆盖 "
                   f"{dates[0] if dates else 'N/A'}~{dates[-1] if dates else 'N/A'}）。"
                   f"stock_zt_pool_em 免费历史仅最近约 3-4 周，更早历史拿不到——样本已退化，"
                   f"结论不可用。勿把此样本当 2 年回测。")
    return {
        "requested_start": _norm_date(start), "requested_end": _norm_date(end),
        "expected_trading_days": expected, "covered_trading_days": covered,
        "coverage_ratio": round(ratio, 3),
        "covered_first": dates[0] if dates else None,
        "covered_last": dates[-1] if dates else None,
        "warning": warning,
    }


def build_event_table(start: str, end: str, use_cache: bool = True) -> Dict[str, Any]:
    """端到端：涨停事件 + 次日 K 线 → 事件表，带本地缓存。覆盖度不足会在结果里高声标注。"""
    cache = data_file("chanlun-backtest", f"event_table_{start}_{end}.json")
    if use_cache:
        cached = read_json(cache, default=None)
        if cached:
            return cached

    raw = fetch_limitup_events(start, end)
    coverage = assess_coverage(raw, start, end)
    if coverage["warning"]:
        print(coverage["warning"], file=sys.stderr)
    codes = [e["code"] for e in raw]
    klines = fetch_klines(codes, days=_auto_days(start))
    events, dropped = assemble_events(raw, klines)
    result = {
        "schema": "daban_bt_event_table_v1",
        "start": start, "end": end,
        "raw_count": len(raw), "event_count": len(events), "dropped": dropped,
        "coverage": coverage,
        "events": events,
        "control_pools": control_pools_from_klines(klines),
    }
    atomic_write_json(cache, result)
    return result
