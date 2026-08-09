"""P2-2: actor claims are represented only by observable proxies."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_four_dim_proxy_values_carry_source_and_asof():
    four_dim = _load("skills/stock-triage/scripts/four_dim_scorer.py", "four_dim_proxy")
    bars = [
        {"date": "2026-08-06", "open": 10, "high": 11, "low": 9.8, "close": 10.5, "volume": 100},
        {"date": "2026-08-07", "open": 10.5, "high": 11.2, "low": 10.2, "close": 11, "volume": 150},
        {"date": "2026-08-08", "open": 11, "high": 11.1, "low": 10.5, "close": 10.8, "volume": 120},
    ]
    result = four_dim.score_technical("600001", "测试", quote={"price": 10.8}, klines=bars)
    proxies = result["observable_proxies"]
    assert proxies["up_down_volume_ratio"]["value"] is not None
    assert proxies["up_down_volume_ratio"]["source"] == "tencent_kline"
    assert proxies["up_down_volume_ratio"]["asof"] == "2026-08-08"
    assert all("source" in item and "asof" in item for item in proxies.values())


def test_candidate_pipeline_missing_proxies_fail_closed():
    pipeline = _load("skills/common/candidate_pipeline.py", "candidate_proxy")
    result = pipeline.compute_price_features([])
    assert result["up_down_volume_ratio"] is None
    assert result["observable_proxies"]["lhb_institution_net_buy"] == {
        "value": None, "source": "unavailable", "asof": None,
    }


def test_daban_evidence_contains_proxy_provenance_and_no_absorption_claim():
    daban = _load("skills/daban-stock-picker/scripts/daban_candidate_api.py", "daban_proxy")
    candidate = daban.example_payload()["candidates"][0]
    candidate.update({
        "up_down_volume_ratio": 1.4,
        "asof": "2026-08-08",
        "source": "fixture",
        "lhb_institution_net_buy": 2.0,
    })
    result = daban.evaluate_candidate(candidate, daban.example_payload()["market"], {})
    proxies = result["observable_proxies"]
    assert result["up_down_volume_ratio"] == 1.4
    assert result["proxy_provenance"]["up_down_volume_ratio"] == {
        "source": "fixture", "asof": "2026-08-08",
    }
    assert result["observable_proxies"]["lhb_institution_net_buy"]["asof_lag_days"] == 1
    assert "已吸筹" not in str(result)


def test_capital_flow_proxy_fields_are_provenance_labeled(monkeypatch):
    cfm = _load("skills/stock-triage/scripts/capital_flow_monitor.py", "capital_proxy")
    monkeypatch.setattr(cfm, "fetch_tencent_flows", lambda _stocks: {
        "600001": {"price": 10.0, "provider": "tencent", "date": "2026-08-08"},
    })
    monkeypatch.setattr(cfm, "fetch_northbound_flow", lambda: {})
    monkeypatch.setattr(cfm, "fetch_sina_northbound_observation", lambda: {
        "status": "error", "provider": "sina", "data": None,
    })
    monkeypatch.setattr(cfm, "fetch_stock_fund_flow", lambda *_args, **_kwargs: {
        "provider": "fixture", "date": "2026-08-07",
        "lhb_institution_net_buy": 1.5,
        "lhb_asof_lag_days": 1,
    })
    monkeypatch.setattr(cfm, "fetch_sector_fund_flow", lambda **_kwargs: {})
    monkeypatch.setattr(cfm, "collect_sector_momentum", lambda **_kwargs: {
        "status": "unavailable", "momentum": None, "rotation": None,
    })
    result = cfm.collect_flow_data(
        stocks=[("600001", "sh", "测试")], sectors=[]
    )
    proxies = result["stocks"][0]["observable_proxies"]
    assert proxies["lhb_institution_net_buy"]["value"] == 1.5
    assert proxies["lhb_institution_net_buy"]["source"] == "fixture"
    assert proxies["lhb_institution_net_buy"]["asof_lag_days"] == 1
    assert all("source" in item and "asof" in item for item in proxies.values())
    assert "已吸筹" not in cfm.format_report(result)
