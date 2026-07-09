"""Canonical market-data adapters for the live D0/D1 selection pipeline."""

from __future__ import annotations

from urllib.parse import urlencode
from typing import Any, Sequence

from a_stock_http import (
    fetch_tencent_kline as _fetch_tencent_kline,
    fetch_tencent_minute as _fetch_tencent_minute,
    fetch_tencent_quote as _fetch_tencent_quote,
    fetch_tencent_snapshot as _fetch_tencent_snapshot,
)
from http_client import DataSourceError, request_bytes


ADAPTER_VERSIONS = {
    "tencent_quote": "tencent-adapter-v2",
    "tencent_kline": "sina-kline-adapter-v1",  # 2026-07-07: 腾讯fqkline已停用(HTTP 501)，改走新浪quotes.sina.cn
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


def fetch_eastmoney_kline(
    code: str,
    *,
    market: str,
    days: int,
    ktype: str = "day",
) -> list[dict[str, Any]]:
    market_id = "1" if market == "sh" else "0"
    klt = {"day": "101", "week": "102", "month": "103"}.get(ktype, "101")
    params = {
        "secid": f"{market_id}.{code}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": klt,
        "fqt": "1",
        "end": "20500101",
        "lmt": str(days),
    }
    import requests

    try:
        session = requests.Session()
        session.trust_env = False
        response = session.get(
            f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{urlencode(params)}",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (Hermes A-Stock Agent)"},
        )
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise DataSourceError("eastmoney_kline", f"历史K线获取失败: {market}{code}", exc) from exc

    rows = ((payload or {}).get("data") or {}).get("klines") or []
    result: list[dict[str, Any]] = []
    for row in rows[-days:]:
        parts = str(row).split(",")
        if len(parts) < 6:
            continue
        try:
            result.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
            })
        except ValueError:
            continue
    return result


def fetch_tencent_minute(code: str, *, market: str) -> list[dict[str, Any]]:
    return _fetch_tencent_minute(code, market=market)


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
