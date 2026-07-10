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
