"""Per-stock fund-flow chain skips content-invalid payloads (2026-09-09 事故回归).

事故：akshare/adata 返回 main_net_yi=NaN 或旧日期的非空 payload，
`_fallback_chain` 只按"空/异常"切换，东财兜底腿从未被尝试，
个股核心观测从 19/23 掉到 12/22，closing-triage 及其下游全部阻断。
"""

from __future__ import annotations

import importlib.util
import json
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


# ── 板块资金流：validated 兑底链 + paper-trading-close 放行 no_positions ──

def _manifest_policy(job_id: str) -> dict:
    manifest = json.load(open(ROOT / "cron" / "hermes-cron-manifest.json"))
    for job in manifest["jobs"]:
        if job.get("id") == job_id:
            return job.get("dependency_policy") or {}
    raise AssertionError(job_id)


def test_paper_trading_close_accepts_no_positions_from_monitor(monkeypatch):
    """模拟账户无持仓（no_positions）是合法的无事可做状态，不得阻断收盘。"""
    from runtime_context import evaluate_dependencies

    policy = _manifest_policy("paper-trading-close")
    artifact = {
        "run_id": "monitor",
        "batch_id": "a-share-20260910",
        "trading_date": "2026-09-10",
        "artifact_path": "/tmp/monitor.json",
        "status": "no_positions",
        "finished_at": "2026-09-10T15:15:00+08:00",
    }
    monkeypatch.setattr(
        "runtime_context.load_latest_artifact", lambda *_a, **_k: artifact
    )

    gate = evaluate_dependencies(
        ["paper-trading-monitor"],
        trading_date="2026-09-10",
        batch_id="a-share-20260910",
        policy=policy,
        now="2026-09-10T15:25:00+08:00",
    )
    assert gate["passed"] is True
    assert "no_positions" in gate["dependencies"][0]["accepted_statuses"]

    for rejected in ("degraded", "blocked"):
        artifact["status"] = rejected
        rejected_gate = evaluate_dependencies(
            ["paper-trading-monitor"],
            trading_date="2026-09-10",
            batch_id="a-share-20260910",
            policy=policy,
            now="2026-09-10T15:25:00+08:00",
        )
        assert rejected_gate["passed"] is False


def test_closing_triage_accepts_partial_from_capital_flow(monkeypatch):
    """北向结构性停披 + 核心观测齐备 → capital-flow=partial 不得阻断收盘链。"""
    from runtime_context import evaluate_dependencies

    policy = _manifest_policy("closing-triage")
    artifact = {
        "run_id": "capital-flow",
        "batch_id": "a-share-20260911",
        "trading_date": "2026-09-11",
        "artifact_path": "/tmp/capital-flow.json",
        "status": "partial",
        "finished_at": "2026-09-11T14:32:00+08:00",
    }
    monkeypatch.setattr(
        "runtime_context.load_latest_artifact", lambda *_a, **_k: artifact
    )

    gate = evaluate_dependencies(
        ["capital-flow"],
        trading_date="2026-09-11",
        batch_id="a-share-20260911",
        policy=policy,
        now="2026-09-11T15:35:00+08:00",
    )
    assert gate["passed"] is True
    assert "partial" in gate["dependencies"][0]["accepted_statuses"]

    for rejected in ("degraded", "blocked"):
        artifact["status"] = rejected
        rejected_gate = evaluate_dependencies(
            ["capital-flow"],
            trading_date="2026-09-11",
            batch_id="a-share-20260911",
            policy=policy,
            now="2026-09-11T15:35:00+08:00",
        )
        assert rejected_gate["passed"] is False


def test_monitor_passes_expected_date_to_sector_fetch(monkeypatch):
    module = _load_monitor()
    monkeypatch.delenv("HERMES_TRADING_DATE", raising=False)
    monkeypatch.setattr(module, "fetch_tencent_flows", lambda stocks: {})
    monkeypatch.setattr(
        module,
        "fetch_northbound_flow",
        lambda: {"date": "2026-09-09", "net_flow_yi": 0.0, "provider": "fixture"},
    )
    captured: dict = {}

    def sector_recorder(bk_code, *, name=None, days=3, expected_date=None):
        captured["expected_date"] = expected_date
        return {}

    monkeypatch.setattr(module, "fetch_sector_fund_flow", sector_recorder)

    result = module.collect_flow_data(
        stocks=[], sectors=[("BK0428", "电力行业")]
    )

    assert captured["expected_date"] == result["quality"]["expected_trading_date"]


def test_sector_chain_skips_mainless_adata_and_lands_on_eastmoney(monkeypatch):
    """adata 板块路由不带 main_net_yi：必须跳过并落到东财兑底腿（BK 码板块）。"""
    _disable_cache(monkeypatch)
    import akshare as ak
    import pandas as pd

    monkeypatch.setattr(
        ak,
        "stock_board_industry_summary_ths",
        lambda: pd.DataFrame([{"板块": "其他", "净流入": 1.0}]),
    )
    import adata as adata_module

    monkeypatch.setattr(
        adata_module.stock.market,
        "get_market_concept_current_east",
        lambda index_code: pd.DataFrame([{"x": 1.0}]),
    )
    east: list = []
    monkeypatch.setattr(
        ma,
        "_fetch_eastmoney_push2_flow",
        lambda secid, days: east.append(secid)
        or {"data": {"klines": ["2026-09-10,1.0,2.0,-500000000.0,4.0,300000.0"]}},
    )

    result = ma.fetch_sector_fund_flow(
        "BK0428", name="电力行业", expected_date="2026-09-10"
    )

    assert east == ["90.BK0428"]
    assert result["provider"] == "eastmoney_push2_degraded"
    assert result["main_net_yi"] == -50000.0


def test_sector_chain_fail_closed_when_all_sources_stale(monkeypatch):
    _disable_cache(monkeypatch)
    import akshare as ak
    import pandas as pd

    monkeypatch.setattr(
        ak,
        "stock_board_industry_summary_ths",
        lambda: pd.DataFrame([{"板块": "其他", "净流入": 1.0}]),
    )
    import adata as adata_module

    monkeypatch.setattr(
        adata_module.stock.market,
        "get_market_concept_current_east",
        lambda index_code: [],
    )
    monkeypatch.setattr(
        ma,
        "_fetch_eastmoney_push2_flow",
        lambda secid, days: {
            "data": {"klines": ["2026-09-09,1.0,2.0,-500000000.0,4.0,300000.0"]}
        },
    )

    result = ma.fetch_sector_fund_flow(
        "BK0428", name="电力行业", expected_date="2026-09-10"
    )

    assert result == {}


def test_sector_legacy_semantics_without_expected_date(monkeypatch):
    """不传 expected_date 的调用方保持旧语义：接受最新非空观测。"""
    _disable_cache(monkeypatch)
    import akshare as ak
    import pandas as pd

    monkeypatch.setattr(
        ak,
        "stock_board_industry_summary_ths",
        lambda: pd.DataFrame([{"板块": "其他", "净流入": 1.0}]),
    )
    import adata as adata_module

    monkeypatch.setattr(
        adata_module.stock.market,
        "get_market_concept_current_east",
        lambda index_code: pd.DataFrame([{"x": 1.0}]),
    )

    result = ma.fetch_sector_fund_flow("BK0428", name="电力行业")

    assert result["provider"] == "adata"
