"""Canonical market-data adapters for the live D0/D1 selection pipeline."""

from __future__ import annotations

from typing import Any, Sequence

from a_stock_http import (
    fetch_tencent_kline as _fetch_tencent_kline,
    fetch_tencent_minute as _fetch_tencent_minute,
    fetch_tencent_quote as _fetch_tencent_quote,
    fetch_tencent_snapshot as _fetch_tencent_snapshot,
)
from http_client import DataSourceError, request_bytes, request_json


ADAPTER_VERSIONS = {
    "tencent_quote": "tencent-adapter-v2",
    "tencent_kline": "tencent-kline-adapter-v2",
    "eastmoney_kline": "eastmoney-kline-adapter-v1",
    "tencent_minute": "tencent-adapter-v1",
    "tencent_orderbook": "tencent-adapter-v2",
    "akshare_limitup": "akshare-adapter-v1",
    "akshare_spot": "akshare-adapter-v1",
    "ths_industry_catalog": "akshare-ths-adapter-v1",
}


def fetch_tencent_quote(codes: Sequence[str]) -> dict[str, dict[str, Any]]:
    return _fetch_tencent_quote(list(codes))


def fetch_tencent_snapshot(codes: Sequence[str]) -> dict[str, dict[str, Any]]:
    return _fetch_tencent_snapshot(list(codes))


def fetch_tencent_kline(
    code: str,
    *,
    market: str,
    days: int,
    ktype: str = "day",
) -> list[dict[str, Any]]:
    return _fetch_tencent_kline(code, market=market, days=days, ktype=ktype)


def fetch_tencent_minute(code: str, *, market: str) -> list[dict[str, Any]]:
    return _fetch_tencent_minute(code, market=market)


def parse_eastmoney_kline_payload(payload: dict[str, Any],
                                  days: int) -> list[dict[str, Any]]:
    """东财日K原始 JSON → 与 fetch_tencent_kline 同构的 bar 列表（纯函数可单测）。

    kline 行形如 "2026-07-09,10.20,10.50,10.60,10.10,123456,..."
    = 日期,开,收,高,低,成交量(手)。
    """
    klines = ((payload or {}).get("data") or {}).get("klines") or []
    bars: list[dict[str, Any]] = []
    for line in klines[-days:]:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        try:
            bars.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
            })
        except (TypeError, ValueError):
            continue
    return bars


def fetch_eastmoney_kline(
    code: str,
    *,
    market: str,
    days: int,
) -> list[dict[str, Any]]:
    """东财前复权日K，腾讯K线失败时的候选池回退源（PR #90 引用但从未实现）。"""
    secid = f"{'1' if market == 'sh' else '0'}.{code}"
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&klt=101&fqt=1&lmt={days}&end=20500101&"
        "fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
    )
    try:
        result = request_json(
            url,
            source="eastmoney_kline",
            timeout=10,
            max_attempts=2,
            headers={"User-Agent": "Mozilla/5.0"},
        )
    except DataSourceError:
        return []
    if not isinstance(result.data, dict):
        return []
    return parse_eastmoney_kline_payload(result.data, days)


def fetch_hot_money_limitup_pool(date: str):
    try:
        import akshare as ak

        return ak.stock_zt_pool_em(date=date)
    except Exception as exc:  # noqa: BLE001
        raise DataSourceError("akshare_limitup", f"涨停池获取失败: {date}", exc) from exc


def fetch_hot_money_strong_pool(date: str):
    try:
        import akshare as ak

        return ak.stock_zt_pool_strong_em(date=date)
    except Exception as exc:  # noqa: BLE001
        raise DataSourceError("akshare_limitup", f"强势股池获取失败: {date}", exc) from exc


def fetch_a_share_spot():
    try:
        import akshare as ak

        return ak.stock_zh_a_spot_em()
    except Exception as exc:  # noqa: BLE001
        raise DataSourceError("akshare_spot", "全A行情获取失败", exc) from exc


def fetch_industry_catalog_ths():
    try:
        import akshare as ak

        return ak.stock_board_industry_name_ths()
    except Exception as exc:  # noqa: BLE001
        raise DataSourceError(
            "ths_industry_catalog",
            "同花顺行业目录获取失败",
            exc,
        ) from exc


def fetch_tencent_index_overview():
    import pandas as pd

    response = request_bytes(
        "http://qt.gtimg.cn/q=sh000001,sz399001,sz399006",
        source="tencent",
        timeout=10,
        max_attempts=2,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    rows = []
    for line in response.data.decode("gbk", errors="ignore").strip().split("\n"):
        if "=" not in line:
            continue
        parts = line.split("=", 1)[1].strip('"').split("~")
        if len(parts) < 40:
            continue
        rows.append({
            "名称": parts[1],
            "代码": parts[2],
            "最新价": float(parts[3]) if parts[3] else 0,
            "涨跌幅": float(parts[32]) if parts[32] else 0,
            "涨跌额": float(parts[31]) if parts[31] else 0,
            "成交额": float(parts[37]) * 10000 if parts[37] else 0,
            "成交量": float(parts[36]) if parts[36] else 0,
            "最高": float(parts[33]) if parts[33] else 0,
            "最低": float(parts[34]) if parts[34] else 0,
            "今开": float(parts[5]) if parts[5] else 0,
            "昨收": float(parts[4]) if parts[4] else 0,
        })
    return pd.DataFrame(rows)
