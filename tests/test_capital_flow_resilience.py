"""Capital-flow degradation preserves semantics and provider provenance."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "skills" / "stock-triage" / "scripts" / "capital_flow_monitor.py"
    spec = importlib.util.spec_from_file_location("capital_flow_resilience", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _failed(provider: str) -> dict:
    return {
        "status": "error",
        "provider": provider,
        "data": None,
        "error": {"type": "network", "message": "unavailable"},
    }


def _stub_adapters(module, monkeypatch):
    """PR #92 后主路由是 market_adapters 韧性链——单元测试必须全部打桩，
    否则会打真网络（曾在 CI 静默期漏进 main）。"""
    monkeypatch.setattr(module, "fetch_northbound_flow", lambda: {})
    monkeypatch.setattr(module, "fetch_stock_fund_flow", lambda code, market=None, days=3: {})
    monkeypatch.setattr(module, "fetch_sector_fund_flow", lambda bk_code, name=None, days=3: {})


def test_northbound_falls_back_to_sina_with_provenance(monkeypatch):
    module = _load()
    _stub_adapters(module, monkeypatch)
    monkeypatch.setattr(module, "fetch_eastmoney_observation", lambda url: _failed("eastmoney"))
    monkeypatch.setattr(
        module,
        "fetch_sina_northbound_observation",
        lambda: {
            "status": "ok",
            "provider": "sina",
            "data": {"date": "2026-06-18", "net_flow_yi": 12.5},
            "error": None,
        },
    )

    result = module.collect_flow_data(stocks=[], sectors=[])

    assert result["northbound"] == {
        "date": "2026-06-18",
        "net_flow_yi": 12.5,
        "provider": "sina",
    }
    assert result["source_health"]["northbound"]["selected_provider"] == "sina"
    assert result["status"] == "degraded"


def test_tencent_volume_metrics_are_labeled_proxy_not_main_flow(monkeypatch):
    module = _load()
    _stub_adapters(module, monkeypatch)
    monkeypatch.setattr(module, "fetch_eastmoney_observation", lambda url: _failed("eastmoney"))
    monkeypatch.setattr(module, "fetch_sina_northbound_observation", lambda: _failed("sina"))
    monkeypatch.setattr(
        module,
        "fetch_tencent_flows",
        lambda stocks: {
            "600001": {
                "price": 10.0,
                "change_pct": 2.0,
                "volume": 100,
                "amount": 200_000_000,
                "turnover": 8.0,
            }
        },
    )

    result = module.collect_flow_data(
        stocks=[("600001", "sh", "demo")],
        sectors=[("BK0001", "demo-sector")],
    )

    stock = result["stocks"][0]
    assert stock["main_flow_status"] == "unavailable"
    assert stock["proxy_metrics"]["provider"] == "tencent"
    assert "main_net_yi" not in stock
    assert result["directional_ready"] is False
    assert result["status"] == "insufficient_data"


def test_tencent_proxy_quotes_are_batched(monkeypatch):
    module = _load()
    calls = []

    class Result:
        data = {
            "sh600001": {"price": 10.0},
            "sz000001": {"price": 11.0},
        }

    def fetch(symbols):
        calls.append(symbols)
        return Result()

    monkeypatch.setattr(module, "fetch_tencent_quotes", fetch)

    result = module.fetch_tencent_flows([
        ("600001", "sh", "one"),
        ("000001", "sz", "two"),
    ])

    assert calls == [["sh600001", "sz000001"]]
    assert result == {"600001": {"price": 10.0}, "000001": {"price": 11.0}}


def test_provider_failures_are_not_reported_as_legitimate_empty(monkeypatch):
    module = _load()
    _stub_adapters(module, monkeypatch)
    monkeypatch.setattr(module, "fetch_eastmoney_observation", lambda url: _failed("eastmoney"))
    monkeypatch.setattr(module, "fetch_sina_northbound_observation", lambda: _failed("sina"))

    result = module.collect_flow_data(stocks=[], sectors=[])

    assert result["status"] == "insufficient_data"
    assert result["directional_ready"] is False
    assert result["source_health"]["northbound"]["attempts"][0]["status"] == "error"
    assert result["source_health"]["northbound"]["attempts"][1]["status"] == "error"
