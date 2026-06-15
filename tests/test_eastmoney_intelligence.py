from datetime import date

import eastmoney_intelligence as em
from http_client import HttpResult


def test_datacenter_query_builds_standard_request(monkeypatch):
    captured = {}

    def fake_request(request, **kwargs):
        captured["url"] = request.full_url
        captured["kwargs"] = kwargs
        return HttpResult(
            {"result": {"data": [{"SECURITY_CODE": "002156"}]}},
            "2026-06-15T08:00:00+00:00",
            1,
        )

    monkeypatch.setattr(em, "_wait_for_provider_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(em, "request_json", fake_request)

    rows = em.datacenter_query(
        "RPT_LIFT_STAGE",
        filter_str='(SECURITY_CODE="002156")',
        sort_columns="FREE_DATE",
    )

    assert rows == [{"SECURITY_CODE": "002156"}]
    assert "reportName=RPT_LIFT_STAGE" in captured["url"]
    assert "filter=%28SECURITY_CODE%3D%22002156%22%29" in captured["url"]
    assert captured["kwargs"]["source"] == "eastmoney"


def test_high_value_endpoints_normalize_fields(monkeypatch):
    fixtures = {
        "RPT_LIFT_STAGE": [{
            "FREE_DATE": "2026-06-20 00:00:00",
            "FREE_SHARES_TYPE": "定向增发机构配售股份",
            "CURRENT_FREE_SHARES": 1200,
            "FREE_RATIO": 0.125,
        }],
        "RPTA_WEB_RZRQ_GGMX": [{
            "DATE": "2026-06-13",
            "RZYE": 100,
            "RZMRE": 20,
            "RZCHE": 10,
            "RQYE": 2,
            "RQMCL": 3,
            "RQCHL": 4,
            "RZRQYE": 102,
        }],
        "RPT_HOLDERNUMLATEST": [{
            "END_DATE": "2026-03-31",
            "HOLDER_NUM": 50000,
            "HOLDER_NUM_CHANGE": -5000,
            "HOLDER_NUM_RATIO": -9.09,
            "AVG_FREE_SHARES": 8000,
        }],
    }

    monkeypatch.setattr(
        em,
        "datacenter_query",
        lambda report_name, **kwargs: fixtures[report_name],
    )

    lockups = em.fetch_lockups("002156", asof=date(2026, 6, 15), forward_days=30)
    margin = em.fetch_margin_trading("002156")
    holders = em.fetch_holder_changes("002156")

    assert lockups["upcoming"][0]["ratio_pct"] == 12.5
    assert lockups["upcoming"][0]["shares"] == 12000000
    assert margin[0]["financing_balance"] == 100
    assert holders[0]["holder_change_pct"] == -9.09


def test_dragon_tiger_handles_stock_without_records(monkeypatch):
    monkeypatch.setattr(em, "datacenter_query", lambda *args, **kwargs: [])

    result = em.fetch_dragon_tiger("002156", asof=date(2026, 6, 15))

    assert result["records"] == []
    assert result["seats"] == {"buy": [], "sell": []}
    assert result["institution"]["net_amount_wan"] == 0.0
