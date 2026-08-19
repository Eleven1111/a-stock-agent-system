"""Regression coverage for the push2-degraded market adapter chain."""

from __future__ import annotations

import pytest

import data_access_config
import market_adapters as ma
from http_client import DataSourceError


def test_fallback_chain_tries_next_provider_after_failure(monkeypatch):
    calls: list[str] = []

    def recorded(provider, endpoint, func):
        calls.append(provider)
        return func()

    monkeypatch.setattr(ma, "_recorded_call", recorded)

    result = ma._fallback_chain(
        "quote",
        (
            ("akshare", lambda: (_ for _ in ()).throw(DataSourceError("akshare", "blocked"))),
            ("adata", lambda: [{"代码": "600519", "名称": "贵州茅台"}]),
        ),
        empty=[],
    )

    assert result == [{"代码": "600519", "名称": "贵州茅台"}]
    assert calls == ["akshare", "adata"]


def test_fallback_chain_fails_closed_when_all_sources_empty(monkeypatch):
    monkeypatch.setattr(ma, "_recorded_call", lambda _provider, _endpoint, func: func())

    with pytest.raises(DataSourceError, match="all providers failed"):
        ma._fallback_chain(
            "fund_flow",
            (("akshare", list), ("adata", dict)),
            empty={},
        )


def test_data_access_config_declares_push2_as_last_resort():
    chains = data_access_config.load_config()["field_chains"]

    assert chains["capital_flow"][:3] == ["akshare", "adata", "eastmoney_datacenter"]
    assert chains["capital_flow"][-2:] == ["eastmoney_push2_degraded", "tencent"]
    assert chains["kline"][-1] == "eastmoney_push2_degraded"


# ── 新浪全A实时行情直连（替代 ak.stock_zh_a_spot）──────────────────

def test_sina_spot_row_maps_full_fields_with_market_cap_in_yuan():
    raw = {
        "symbol": "sh600519", "code": "600519", "name": "贵州茅台",
        "trade": "1450.00", "pricechange": "10.00", "changepercent": "0.69",
        "buy": "1449.00", "sell": "1450.00", "settlement": "1440.00",
        "open": "1440.00", "high": "1455.00", "low": "1438.00",
        "volume": "100000", "amount": "1450000000", "ticktime": "15:00:00",
        "per": 30.5, "pb": 8.2, "mktcap": 18215000.0, "nmc": 18215000.0,
        "turnoverratio": 0.35,
    }
    rec = ma._sina_spot_row_to_record(raw)
    assert rec["代码"] == "sh600519"
    assert rec["名称"] == "贵州茅台"
    assert rec["市盈率-动态"] == 30.5
    assert rec["市净率"] == 8.2
    # 新浪 mktcap 单位是万元，必须转成元，与 eod 扫描器的 *1e8 口径一致
    assert rec["总市值"] == 18215000.0 * 10000
    assert rec["流通市值"] == 18215000.0 * 10000
    assert rec["换手率"] == 0.35


def test_sina_spot_row_tolerates_missing_optional_fields():
    rec = ma._sina_spot_row_to_record(
        {"symbol": "sz000001", "name": "平安银行", "trade": "11.25"}
    )
    assert rec["代码"] == "sz000001"
    assert rec["总市值"] is None
    assert rec["市盈率-动态"] is None
    assert rec["最新价"] == 11.25


def test_sina_spot_pagination_stops_at_short_page():
    pages = iter(
        [
            [{"代码": f"sh{i:06d}", "名称": f"s{i}"} for i in range(100)],
            [{"代码": "sh600519", "名称": "贵州茅台"}],   # 短页 = 末页
        ]
    )
    records = ma._fetch_sina_spot(
        lambda page, size: next(pages), page_size=100, max_pages=10
    )
    assert len(records) == 101
    assert records[-1]["名称"] == "贵州茅台"


def test_sina_spot_raises_when_a_page_fails():
    def flaky(page, size):
        if page == 2:
            raise DataSourceError("sina_spot_direct", "page 2 down")
        return [{"代码": f"sh{i:06d}"} for i in range(100)]   # 满页才会继续到第 2 页

    with pytest.raises(DataSourceError):
        ma._fetch_sina_spot(flaky, page_size=100, max_pages=10)


def test_sina_spot_fails_closed_on_empty():
    with pytest.raises(DataSourceError, match="为空"):
        ma._fetch_sina_spot(lambda page, size: [], page_size=100, max_pages=10)
