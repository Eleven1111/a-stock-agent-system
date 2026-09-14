"""fuyao 同花顺官方 API 接入回归测试（客户端信封/限流退避/涨停池归一化/兜底链）。"""

from __future__ import annotations

from types import SimpleNamespace

import fuyao_client as fc
import market_adapters as ma
import pandas as pd
import pytest
from http_client import DataSourceError


def _ok(payload):
    return SimpleNamespace(data=payload)


def test_client_returns_rows_on_success(monkeypatch):
    monkeypatch.setenv("FUYAO_API_KEY", "test-key")
    seen: dict = {}

    def fake_request_json(url, *, source=None, headers=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers
        return _ok({"code": 0, "data": {"item": [{"thscode": "000001.SZ"}]}})

    monkeypatch.setattr(fc, "request_json", fake_request_json)

    rows = fc.limit_up_pool("20260912")

    assert rows == [{"thscode": "000001.SZ"}]
    assert seen["headers"]["X-api-key"] == "test-key"
    assert "limit-up-pool" in seen["url"]
    assert "trade_date=20260912" in seen["url"]


def test_client_retries_on_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setenv("FUYAO_API_KEY", "test-key")
    calls: list = []

    def fake_request_json(url, *, source=None, headers=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            raise DataSourceError("fuyao", "HTTP 429 rate limited")
        return _ok({"code": 0, "data": {"item": []}})

    monkeypatch.setattr(fc, "request_json", fake_request_json)
    monkeypatch.setattr(fc.time, "sleep", lambda _s: None)

    assert fc.limit_up_pool("20260912") == []
    assert len(calls) == 2


def test_client_fails_closed_on_business_error(monkeypatch):
    monkeypatch.setenv("FUYAO_API_KEY", "test-key")
    monkeypatch.setattr(
        fc,
        "request_json",
        lambda *a, **k: _ok({"code": 2001, "message": "unauthorized", "data": None}),
    )

    with pytest.raises(DataSourceError, match="2001"):
        fc.limit_up_pool("20260912")


def test_client_fails_closed_without_key(monkeypatch):
    monkeypatch.delenv("FUYAO_API_KEY", raising=False)
    with pytest.raises(DataSourceError, match="FUYAO_API_KEY"):
        fc.limit_up_pool("20260912")


def test_adapter_normalizes_fuyao_rows(monkeypatch):
    rows = [{
        "ticker": "688835", "name": "高凯技术", "continue_day_cnt": 1,
        "seal_money": 233765950, "limit_up_time": "09:58",
        "limit_up_reason": "半导体设备", "last_price": 324.41,
        "price_change_ratio_pct": 20.0007,
    }]
    monkeypatch.setattr("fuyao_client.limit_up_pool", lambda trade_date: rows)
    monkeypatch.setattr(
        "industry_map.load_cached", lambda asof, **k: {"688835": "半导体"}
    )

    df = ma.fetch_fuyao_limitup_pool("2026-09-12")

    assert df.iloc[0]["代码"] == "688835"
    assert df.iloc[0]["连板数"] == 1
    assert df.iloc[0]["所属行业"] == "半导体"
    assert df.iloc[0]["封板资金"] == 233765950
    assert df.iloc[0]["首次封板时间"] == "09:58"


def test_hot_money_pool_prefers_fuyao(monkeypatch):
    rows = [{"ticker": "688835", "name": "高凯技术", "continue_day_cnt": 1,
             "seal_money": 1.0, "limit_up_time": "09:58"}]
    monkeypatch.setattr("fuyao_client.limit_up_pool", lambda trade_date: rows)

    def _boom(date):
        raise AssertionError("fuyao 成功时不得调用 akshare 兜底")

    import akshare as ak

    monkeypatch.setattr(ak, "stock_zt_pool_em", _boom)

    df = ma.fetch_hot_money_limitup_pool("20260912")

    assert df.iloc[0]["代码"] == "688835"


def test_hot_money_pool_falls_back_to_akshare(monkeypatch):
    import akshare as ak

    def _fuyao_boom(trade_date):
        raise DataSourceError("fuyao_ths_api", "network down")

    monkeypatch.setattr("fuyao_client.limit_up_pool", _fuyao_boom)
    fallback = pd.DataFrame([{"代码": "600519", "名称": "贵州茅台", "连板数": 1}])
    monkeypatch.setattr(ak, "stock_zt_pool_em", lambda date: fallback)

    df = ma.fetch_hot_money_limitup_pool("20260912")

    assert df.iloc[0]["代码"] == "600519"
