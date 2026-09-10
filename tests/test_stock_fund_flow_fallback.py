"""Per-stock fund-flow chain skips content-invalid payloads (2026-09-09 事故回归).

事故：akshare/adata 返回 main_net_yi=NaN 或旧日期的非空 payload，
`_fallback_chain` 只按"空/异常"切换，东财兜底腿从未被尝试，
个股核心观测从 19/23 掉到 12/22，closing-triage 及其下游全部阻断。
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import market_adapters as ma


ROOT = Path(__file__).resolve().parents[1]


def _load_monitor():
    path = ROOT / "skills" / "stock-triage" / "scripts" / "capital_flow_monitor.py"
    spec = importlib.util.spec_from_file_location("cfc_fallback_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _disable_cache(monkeypatch):
    monkeypatch.setattr(ma, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(ma, "_cache_set", lambda *a, **k: None)


def _install_legs(monkeypatch, *, akshare_day, eastmoney_day, eastmoney_main):
    """akshare(个股+THS榜) → adata → eastmoney_push2 四条腿全部打桩。"""
    import akshare as ak
    import pandas as pd

    monkeypatch.setattr(
        ak,
        "stock_individual_fund_flow",
        lambda stock, market: pd.DataFrame([
            {"日期": akshare_day, "主力净流入-净额": 5000.0, "小单净流入-净额": 100.0},
        ]),
    )
    monkeypatch.setattr(ak, "stock_fund_flow_individual", lambda symbol: [])
    import adata as adata_module

    monkeypatch.setattr(
        adata_module.stock.market, "get_capital_flow", lambda code: []
    )
    eastmoney_calls = []
    monkeypatch.setattr(
        ma,
        "_fetch_eastmoney_push2_flow",
        lambda secid, days: eastmoney_calls.append(secid)
        or {"data": {"klines": [f"{eastmoney_day},1.0,2.0,{eastmoney_main},4.0,300000.0"]}},
    )
    return eastmoney_calls


def test_chain_skips_stale_primary_and_lands_on_eastmoney(monkeypatch):
    _disable_cache(monkeypatch)
    calls = _install_legs(
        monkeypatch, akshare_day="2026-09-08", eastmoney_day="2026-09-09",
        eastmoney_main=-800000000.0,
    )

    result = ma.fetch_stock_fund_flow("600519", market="sh", expected_date="2026-09-09")

    # akshare 返回的是昨日观测（stale），必须被跳过并落到东财兜底腿
    assert calls == ["1.600519"]
    assert result["provider"] == "eastmoney_push2_degraded"
    assert result["date"] == "2026-09-09"
    assert result["main_net_yi"] == -80000.0


def test_chain_fail_closed_when_no_source_has_current_day(monkeypatch):
    _disable_cache(monkeypatch)
    _install_legs(
        monkeypatch, akshare_day="2026-09-08", eastmoney_day="2026-09-08",
        eastmoney_main=-800000000.0,
    )

    result = ma.fetch_stock_fund_flow("600519", market="sh", expected_date="2026-09-09")

    assert result == {}


def test_primary_valid_day_short_circuits_fallback(monkeypatch):
    _disable_cache(monkeypatch)
    calls = _install_legs(
        monkeypatch, akshare_day="2026-09-09", eastmoney_day="2026-09-09",
        eastmoney_main=-800000000.0,
    )

    result = ma.fetch_stock_fund_flow("600519", market="sh", expected_date="2026-09-09")

    assert result["provider"] == "akshare"
    assert result["main_net_yi"] == 5e-05  # 5000.0 元 → 0.00005 亿
    assert calls == []  # 主源当日有效时不得发起东财兜底请求


def test_legacy_semantics_without_expected_date(monkeypatch):
    """不传 expected_date 的调用方（探针类）保持旧语义：接受最新非空观测。"""
    _disable_cache(monkeypatch)
    _install_legs(
        monkeypatch, akshare_day="2026-09-08", eastmoney_day="2026-09-09",
        eastmoney_main=-800000000.0,
    )

    result = ma.fetch_stock_fund_flow("600519", market="sh")

    assert result["provider"] == "akshare"
    assert result["date"] == "2026-09-08"


def test_flow_payload_validator():
    today = date.today().isoformat()
    assert ma._flow_payload_matches_date(
        {"main_net_yi": 1.0, "date": today}, today
    ) is True
    assert ma._flow_payload_matches_date(
        {"main_net_yi": float("nan"), "date": today}, today
    ) is False
    assert ma._flow_payload_matches_date(
        {"main_net_yi": 1.0, "date": "2026-09-08"}, today
    ) is False
    assert ma._flow_payload_matches_date({}, today) is False
    assert ma._flow_payload_matches_date(
        {"main_net_yi": 1.0, "date": "bad-date"}, today
    ) is False


def test_monitor_passes_expected_trading_date(monkeypatch):
    module = _load_monitor()
    monkeypatch.delenv("HERMES_TRADING_DATE", raising=False)
    monkeypatch.setattr(module, "fetch_tencent_flows", lambda stocks: {})
    monkeypatch.setattr(
        module,
        "fetch_northbound_flow",
        lambda: {"date": "2026-09-09", "net_flow_yi": 0.0, "provider": "fixture"},
    )
    captured: dict = {}

    def recorder(code, *, market=None, days=3, expected_date=None):
        captured["expected_date"] = expected_date
        return {"date": "2026-09-09", "main_net_yi": 1.0, "retail_net_yi": 0.5,
                "provider": "fixture"}

    monkeypatch.setattr(module, "fetch_stock_fund_flow", recorder)

    result = module.collect_flow_data(
        stocks=[("600519", "sh", "贵州茅台")], sectors=[]
    )

    assert captured["expected_date"] is not None
    assert captured["expected_date"] == result["quality"]["expected_trading_date"]
