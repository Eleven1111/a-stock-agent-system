#!/usr/bin/env python3
"""
打板回测 — 数据层（历史涨停事件 + 次日 K 线 → 事件表）
========================================================
默认 source="akshare" 逐日拉 stock_zt_pool_em（不走 push2，TUN 下可用）取涨停事件，
但免费历史仅最近约 3-4 周；source="mootdx" 走通达信 TCP 全市场日线重建，深历史 6 年+
（仅 H1 gap 假设，first_seal 等盘口字段为 None，见 fetch_limitup_events / mootdx_source）。
收集代码后批量拉日线（akshare→腾讯 ifzq，mootdx→同源同深度），把 T 收 / T+1 开 / T+1 收 join 进事件表。

为保证 gap 口径一致，t_close / t1_open / t1_close 统一取自同一份（qfq）K 线，
zt_pool 只负责事件筛选与 first_seal/连板/封单等元数据。

纯函数（kline_lookup / assemble_events）可用合成数据单测；
触网函数（fetch_limitup_events / fetch_klines）手动冒烟。
"""

import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
import execution_constraints as xc  # noqa: E402
from a_stock_http import fetch_tencent_kline, DataSourceError  # noqa: E402
from state_store import read_json, atomic_write_json  # noqa: E402
from paths import data_file  # noqa: E402


# v3 相对 v2 增补 T 日 OHLCV/成交额与 t_prev_close/t1_amount —— P5(a) 成交约束模型
# 判「一字禁买 / 回封参与率 / 跌停承接量」必需的字段。schema 提级同时让 v2 旧缓存
# 自动失效重建（引擎对缺字段是 fail-closed 拒绝成交，静默复用 v2 会让样本清零）。
EVENT_SCHEMA = "daban_bt_event_table_v3"


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


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bar_amount(bar: Dict[str, Any]) -> Optional[float]:
    """日线自带的成交额（元）。腾讯 ifzq 日线不带 amount → None，
    由 execution_constraints 用 volume×close×每手股数单口径折算，避免两处各折算一次。"""
    direct = _float_or_none(bar.get("amount"))
    return direct if direct is not None and direct > 0 else None


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


def kline_pair_lookup(
    kline: List[Dict[str, Any]], date: str
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Return complete signal-day and T+1 bars for execution checks."""
    target = _norm_date(date)
    for index, bar in enumerate(kline):
        if _norm_date(bar.get("date")) == target:
            if index + 1 >= len(kline):
                return None
            return dict(bar), dict(kline[index + 1])
    return None


def first_sellable_exit(
    kline: List[Dict[str, Any]],
    entry_index: int,
    code: str,
    name: str,
    minimum_holding_sessions: int = 1,
) -> Optional[Tuple[Dict[str, Any], int]]:
    """Find the first close that can be sold after the A-share T+1 boundary.

    P5(a) 3：跌停日不是只有「一字跌停」才卖不掉——跌停价上没有承接量同样成交不了，
    一律顺延到次一可成交时点（与 T+1 叠加）。判定走 execution_constraints，
    涨跌停价按事件日期取制度（P5(b)），停牌/缺量同样顺延。
    """
    start = entry_index + minimum_holding_sessions
    is_st = "ST" in str(name or "").upper()
    for index in range(start, len(kline)):
        bar = kline[index]
        if float(bar.get("volume", 0) or 0) <= 0:
            continue
        previous_close = float(kline[index - 1].get("close", 0) or 0)
        if previous_close <= 0:
            continue
        verdict = xc.assess_sell_fill(
            dict(bar),
            code=str(code).zfill(6),
            asof=_norm_date(bar.get("date")),
            prev_close=previous_close,
            is_st=is_st,
        )
        if not verdict["filled"]:
            continue
        return dict(bar), index - entry_index
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
        pair = kline_pair_lookup(kline, ev.get("date"))
        if pair is None:
            dropped["no_next_day"] += 1
            continue
        current, nxt = pair
        entry_index = next(
            index for index, bar in enumerate(kline)
            if _norm_date(bar.get("date")) == _norm_date(nxt.get("date"))
        )
        sellable = first_sellable_exit(
            kline,
            entry_index,
            code,
            str(ev.get("name") or ""),
        )
        if sellable is None:
            dropped.setdefault("no_sellable_exit", 0)
            dropped["no_sellable_exit"] += 1
            continue
        exit_bar, holding_sessions = sellable
        t_close = float(current["close"])
        t1_open = float(nxt["open"])
        t1_close = float(nxt["close"])
        # T 日的前一根（昨收）——算 T 日涨跌停价必需；缺则留 None 由引擎 fail-closed。
        prev_index = entry_index - 2
        t_prev_close = (
            float(kline[prev_index].get("close"))
            if prev_index >= 0 and kline[prev_index].get("close")
            else None
        )
        out.append({
            "code": code,
            "name": ev.get("name", code),
            "date": _norm_date(ev.get("date")),
            "t_close": t_close,
            "t1_open": t1_open,
            "t1_close": t1_close,
            "entry_date": _norm_date(nxt.get("date")),
            # v3：T 日成交约束字段（一字禁买 / 回封参与率）
            "t_prev_close": t_prev_close,
            "t_open": _float_or_none(current.get("open")),
            "t_high": _float_or_none(current.get("high")),
            "t_low": _float_or_none(current.get("low")),
            "t_volume": _float_or_none(current.get("volume")),
            "t_amount": _bar_amount(current),
            "t1_high": float(nxt.get("high", max(t1_open, t1_close))),
            "t1_low": float(nxt.get("low", min(t1_open, t1_close))),
            "t1_volume": float(nxt.get("volume", 0) or 0),
            "t1_amount": _bar_amount(nxt),
            "exit_date": _norm_date(exit_bar.get("date")),
            "exit_close": float(exit_bar["close"]),
            "holding_sessions": holding_sessions,
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


def fetch_limitup_events(start: str, end: str, sleep: float = 0.3,
                         source: str = "akshare") -> List[Dict[str, Any]]:
    """
    历史涨停事件。start/end 形如 '20260301'。
    source="akshare"（默认）：逐日 stock_zt_pool_em，元数据全（含 first_seal/封单），
        但免费历史仅最近约 3-4 周——深历史会退化（assess_coverage 告警）。
    source="mootdx"：通达信 TCP 全市场日线重建，历史 6 年+，但仅 code/date/lianban，
        first_seal/seal_amount/sector 为 None——只够 H1 gap 假设，H2 真竞价封不可用。
    """
    if source == "mootdx":
        sys.path.insert(0, os.path.dirname(__file__))
        from mootdx_source import reconstruct_limitup_events  # noqa: E402

        return reconstruct_limitup_events(_norm_date(start), _norm_date(end))

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


def build_event_table(start: str, end: str, use_cache: bool = True,
                      source: str = "akshare") -> Dict[str, Any]:
    """
    端到端：涨停事件 + 次日 K 线 → 事件表，带本地缓存。覆盖度不足会在结果里高声标注。
    source="akshare"（默认）：元数据全但仅最近 3-4 周；K 线走腾讯 ifzq。
    source="mootdx"：通达信深历史重建，事件与 K 线同源同深度（6 年+），仅 H1 gap 假设可用。
    """
    cache = data_file("chanlun-backtest", f"event_table_{source}_{start}_{end}.json")
    if use_cache:
        cached = read_json(cache, default=None)
        if isinstance(cached, dict) and cached.get("schema") == EVENT_SCHEMA:
            return cached

    raw = fetch_limitup_events(start, end, source=source)
    coverage = assess_coverage(raw, start, end)
    if coverage["warning"]:
        print(coverage["warning"], file=sys.stderr)
    codes = [e["code"] for e in raw]
    if source == "mootdx":
        sys.path.insert(0, os.path.dirname(__file__))
        from mootdx_source import fetch_klines as fetch_klines_mootdx  # noqa: E402

        klines = fetch_klines_mootdx(codes, _norm_date(start))
    else:
        klines = fetch_klines(codes, days=_auto_days(start))
    events, dropped = assemble_events(raw, klines)
    result = {
        "schema": EVENT_SCHEMA,
        "source": source,
        "start": start, "end": end,
        "raw_count": len(raw), "event_count": len(events), "dropped": dropped,
        "coverage": coverage,
        "events": events,
        "control_pools": control_pools_from_klines(klines),
    }
    atomic_write_json(cache, result)
    return result
