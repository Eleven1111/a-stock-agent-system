#!/usr/bin/env python3
"""
打板回测 — mootdx 深历史数据源（通达信 TCP，免代理，历史 6 年+）
================================================================
解决 akshare.stock_zt_pool_em 免费历史仅最近约 3-4 周的硬墙：用 mootdx 拉全市场
个股多年日线，本地按「收盘价 == 涨停价」重建历史涨停事件，输出与
daban_bt_data._map_zt_row 同构的标准化事件，供 H1 gap 回测消费。

价值边界（诚实声明，勿夸大）：
- ✅ H1 gap 假设（涨停 → 次日跳空，纯 OHLC）：mootdx 完整支撑 2 年+ 回测。
- ❌ H2 真竞价封 ≤09:25（需「首次封板时间」=盘中分笔）：日线拿不到，mootdx 无能为力。
     重建事件的 first_seal / seal_amount / float_mktcap / sector 一律为 None，
     依赖这些字段的检验不可用此数据源。

纯函数（limit_ratio / limit_cap / detect_limitups / to_kline / is_a_stock）可合成
数据单测；触网函数（get_client / list_a_stocks / fetch_daily /
reconstruct_limitup_events）手动冒烟。本机 ClashX TUN 模式下 TCP 直连通达信实测
可用（历史深至 2019-11，单只 800 日线约 0.04s）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# mootdx frequency 编码：9 = 日线（实测）。
_DAILY = 9
# mootdx bars 单次返回上限。
_PAGE = 800
# 浮点容差：收盘价与涨停价之差 < TOL 即视为封板涨停。
_TOL = 0.005

_CLIENT = None  # 进程内单例，避免每次 factory 重连（首连约 8s）。


# --------------------------------------------------------------------------- #
# 纯函数（可合成数据单测）
# --------------------------------------------------------------------------- #
def is_a_stock(code: str) -> bool:
    """真正的 A 股个股代码？排除指数 / 基金 / 债券 / 板块（mootdx stocks 列表混入大量非个股）。"""
    code = str(code)
    return code.startswith((
        "600", "601", "603", "605", "688",        # 沪：主板 + 科创
        "000", "001", "002", "003", "300", "301",  # 深：主板 + 创业
        "8", "4", "920",                            # 北交所
    ))


def limit_ratio(code: str, name: str = "") -> float:
    """涨停幅度：ST 5%，创业板 / 科创板 20%，北交所 30%，其余主板 10%。"""
    if "ST" in str(name).upper():
        return 0.05
    code = str(code)
    if code.startswith(("300", "301", "688")):
        return 0.20
    if code.startswith(("8", "4", "920")):
        return 0.30
    return 0.10


def limit_cap(prev_close: float, code: str, name: str = "") -> float:
    """昨收 → 当日涨停价（交易所口径：四舍五入到分）。"""
    return round(float(prev_close) * (1 + limit_ratio(code, name)), 2)


def to_kline(df: Any) -> List[Dict[str, Any]]:
    """mootdx bars DataFrame → 标准 kline dict 列表（对齐 fetch_tencent_kline 字段，升序）。"""
    if df is None or len(df) == 0:
        return []
    out: List[Dict[str, Any]] = []
    for row in df.to_dict("records"):
        dt = str(row.get("datetime", ""))[:10]
        if not dt:
            continue
        out.append({
            "date": dt,
            "open": float(row.get("open", 0) or 0),
            "close": float(row.get("close", 0) or 0),
            "high": float(row.get("high", 0) or 0),
            "low": float(row.get("low", 0) or 0),
            "volume": float(row.get("vol", row.get("volume", 0)) or 0),
        })
    return out


def detect_limitups(kline: List[Dict[str, Any]], code: str, name: str = "",
                    tol: float = _TOL) -> List[Dict[str, Any]]:
    """
    升序日线序列 → 涨停事件日列表 [{date, lianban}]。
    口径：收盘价封死涨停（close >= 涨停价 - tol）；lianban = 连续涨停天数（断板归零）。
    注意：停牌会令相邻 bar 跨越缺口，连板计数在停牌处可能偏高，属已知边缘情况。
    """
    hits: List[Dict[str, Any]] = []
    streak = 0
    for i in range(1, len(kline)):
        prev_close = float(kline[i - 1].get("close", 0) or 0)
        close = float(kline[i].get("close", 0) or 0)
        if prev_close <= 0:
            streak = 0
            continue
        if close >= limit_cap(prev_close, code, name) - tol:
            streak += 1
            hits.append({"date": str(kline[i].get("date", "")), "lianban": streak})
        else:
            streak = 0
    return hits


def standardize_event(code: str, name: str, hit: Dict[str, Any]) -> Dict[str, Any]:
    """重建事件 → 与 daban_bt_data._map_zt_row 同构的标准化原始事件（盘口元数据为 None）。"""
    name = str(name)
    return {
        "code": str(code).zfill(6),
        "name": name,
        "date": str(hit.get("date", "")),
        "first_seal": None,      # 盘口数据，日线拿不到
        "lianban": hit.get("lianban"),
        "seal_amount": None,     # 盘口数据，日线拿不到
        "float_mktcap": None,    # 日线拿不到（需 F10）
        "sector": None,          # 日线拿不到（需行业分类源）
        "is_st": "ST" in name.upper(),
    }


# --------------------------------------------------------------------------- #
# 触网函数（手动冒烟）
# --------------------------------------------------------------------------- #
def get_client(timeout: int = 8):
    """进程内单例 mootdx 标准市场 client。首次连接会选服务器（约 8s）。"""
    global _CLIENT
    if _CLIENT is None:
        from mootdx.quotes import Quotes
        _CLIENT = Quotes.factory(market="std", timeout=timeout)
    return _CLIENT


def list_a_stocks(client: Any = None) -> List[Dict[str, str]]:
    """全市场真实个股 [{code, name}]（沪 market=1 + 深 market=0，过滤掉指数/基金/板块）。"""
    client = client or get_client()
    out: List[Dict[str, str]] = []
    seen = set()
    for market in (1, 0):
        df = client.stocks(market=market)
        if df is None:
            continue
        for row in df.to_dict("records"):
            code = str(row.get("code", "")).zfill(6)
            if code in seen or not is_a_stock(code):
                continue
            seen.add(code)
            out.append({"code": code, "name": str(row.get("name", "")).strip()})
    return out


def fetch_daily(code: str, start_date: str, client: Any = None,
                max_pages: int = 4) -> List[Dict[str, Any]]:
    """单只票日线，自动向前分页直到覆盖 start_date 或翻到历史尽头。返回升序去重 kline。"""
    client = client or get_client()
    by_date: Dict[str, Dict[str, Any]] = {}
    for page in range(max_pages):
        df = client.bars(symbol=str(code), frequency=_DAILY, offset=_PAGE, start=page * _PAGE)
        bars = to_kline(df)
        if not bars:
            break
        for bar in bars:
            by_date.setdefault(bar["date"], bar)
        if min(by_date) <= start_date:   # 已覆盖到请求起点
            break
    return [by_date[d] for d in sorted(by_date)]


def fetch_klines(codes: Sequence[str], start_date: str, client: Any = None,
                 max_pages: int = 4) -> Dict[str, List[Dict[str, Any]]]:
    """批量拉各 code 日线（复用 fetch_daily），供 assemble_events join。
    与重建事件同源同深度，避免跨源（mootdx 事件 vs 腾讯 K 线）深度不匹配丢事件。失败跳过。"""
    client = client or get_client()
    out: Dict[str, List[Dict[str, Any]]] = {}
    for code in sorted({str(c).zfill(6) for c in codes}):
        try:
            kline = fetch_daily(code, start_date, client=client, max_pages=max_pages)
        except Exception:   # noqa: BLE001 — 单票失败不影响其余
            continue
        if kline:
            out[code] = kline
    return out


def reconstruct_limitup_events(
    start: str, end: str, *,
    client: Any = None,
    universe: Optional[Sequence[Dict[str, str]]] = None,
    max_pages: int = 4,
) -> List[Dict[str, Any]]:
    """
    全市场重建 [start, end] 区间历史涨停事件（标准化格式，供 assemble_events 消费）。
    start/end 形如 '2024-06-01'。串行约 3 分钟 / 5400 只；结果应由上层落缓存复用。
    universe 可注入（测试用），默认 list_a_stocks 全量。
    """
    client = client or get_client()
    pool = list(universe) if universe is not None else list_a_stocks(client)
    events: List[Dict[str, Any]] = []
    for item in pool:
        code, name = item["code"], item.get("name", "")
        try:
            kline = fetch_daily(code, start, client=client, max_pages=max_pages)
        except Exception:   # noqa: BLE001 — 单票失败不应中断全市场重建
            continue
        for hit in detect_limitups(kline, code, name):
            if start <= hit["date"] <= end:
                events.append(standardize_event(code, name, hit))
    return events
