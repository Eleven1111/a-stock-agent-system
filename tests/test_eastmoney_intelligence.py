from datetime import date
import json
import os
import threading
import time

import eastmoney_intelligence as em
import pytest
from http_client import DataSourceError, ErrorType, HttpResult


@pytest.fixture(autouse=True)
def isolated_provider_state(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))


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


def test_event_and_institution_endpoints_share_datacenter_normalization(monkeypatch):
    fixtures = {
        "RPT_SHAREBONUS_DET": [{
            "PRETAX_BONUS_RMB": 2.5,
            "EX_DIVIDEND_DATE": "2026-06-20 00:00:00",
            "EQUITY_RECORD_DATE": "2026-06-19 00:00:00",
            "PLAN_NOTICE_DATE": "2026-05-30 00:00:00",
            "ASSIGN_PROGRESS": "实施",
        }],
        "RPT_ORG_SURVEY": [{
            "NOTICE_DATE": "2026-06-14",
            "RECEPTIONAMOUNT": 12,
            "MAINPOINT": "关注先进封装进展",
        }],
        "RPT_HOLDER_TRADE_STOCK": [{
            "NOTICE_DATE": "2026-06-13",
            "PARTICIPANTNAME": "控股股东",
            "TRADETYPE": "2",
            "TRADENUM": 100000,
        }],
    }
    monkeypatch.setattr(
        em,
        "datacenter_query",
        lambda report_name, **kwargs: fixtures[report_name],
    )

    dividend = em.fetch_dividend("002156", asof="2026-06-15")
    visits = em.fetch_research_visits("002156")
    trades = em.fetch_insider_trades("002156")

    assert dividend["is_upcoming"] is True
    assert visits == [{
        "date": "2026-06-14",
        "org_count": 12.0,
        "summary": "关注先进封装进展",
    }]
    assert trades[0]["direction"] == "减持"


def test_dragon_tiger_handles_stock_without_records(monkeypatch):
    monkeypatch.setattr(em, "datacenter_query", lambda *args, **kwargs: [])

    result = em.fetch_dragon_tiger("002156", asof=date(2026, 6, 15))

    assert result["records"] == []
    assert result["seats"] == {"buy": [], "sell": []}
    assert result["institution"]["net_amount_wan"] == 0.0


def test_datacenter_business_failure_is_not_silently_treated_as_empty(monkeypatch):
    monkeypatch.setattr(em, "_wait_for_provider_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        em,
        "request_json",
        lambda *args, **kwargs: HttpResult(
            {"success": False, "code": 500, "message": "server busy"},
            "2026-06-15T08:00:00+00:00",
            1,
        ),
    )

    with pytest.raises(DataSourceError) as caught:
        em.datacenter_query("RPT_LIFT_STAGE")

    assert caught.value.error_type == ErrorType.INVALID_RESPONSE
    assert "server busy" in caught.value.message


def test_datacenter_legal_empty_result_remains_valid(monkeypatch):
    monkeypatch.setattr(em, "_wait_for_provider_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        em,
        "request_json",
        lambda *args, **kwargs: HttpResult(
            {"success": True, "code": 0, "result": {"data": []}},
            "2026-06-15T08:00:00+00:00",
            1,
        ),
    )

    assert em.datacenter_query("RPT_LIFT_STAGE") == []


def test_datacenter_known_no_data_business_code_remains_valid(monkeypatch):
    monkeypatch.setattr(em, "_wait_for_provider_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        em,
        "request_json",
        lambda *args, **kwargs: HttpResult(
            {"success": False, "code": 9201, "message": "返回数据为空"},
            "2026-06-15T08:00:00+00:00",
            1,
        ),
    )

    assert em.datacenter_query("RPT_LIFT_STAGE") == []


def test_each_retry_reenters_shared_rate_limit_and_uses_backoff(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    slots = []
    sleeps = []
    responses = iter([
        DataSourceError(
            "eastmoney",
            "HTTP 429",
            error_type=ErrorType.HTTP,
            status_code=429,
        ),
        HttpResult(
            {"success": True, "code": 0, "result": {"data": []}},
            "2026-06-15T08:00:01+00:00",
            1,
        ),
    ])

    monkeypatch.setattr(
        em,
        "_wait_for_provider_slot",
        lambda *args, **kwargs: slots.append("slot"),
    )
    monkeypatch.setattr(em.time, "sleep", lambda delay: sleeps.append(delay))

    def fake_request(*args, **kwargs):
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(em, "request_json", fake_request)

    assert em.datacenter_query("RPT_LIFT_STAGE") == []
    assert slots == ["slot", "slot"]
    assert len(sleeps) == 1
    assert sleeps[0] > 0


def test_circuit_breaker_blocks_calls_after_repeated_provider_failures(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        em,
        "_settings",
        lambda: {
            "timeout_seconds": 1,
            "max_attempts": 1,
            "minimum_interval_seconds": 0,
            "jitter_max_seconds": 0,
            "backoff_base_seconds": 0.01,
            "circuit_failure_threshold": 2,
            "circuit_open_seconds": 60,
            "coordination_backend": "shared_file",
            "coordination_timeout_seconds": 1,
            "coordination_stale_seconds": 10,
        },
    )
    monkeypatch.setattr(em, "_wait_for_provider_slot", lambda *args, **kwargs: None)
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise DataSourceError(
            "eastmoney",
            "HTTP 503",
            error_type=ErrorType.HTTP,
            status_code=503,
        )

    monkeypatch.setattr(em, "request_json", fail)

    for _ in range(2):
        with pytest.raises(DataSourceError):
            em.datacenter_query("RPT_LIFT_STAGE")
    with pytest.raises(DataSourceError, match="circuit"):
        em.datacenter_query("RPT_LIFT_STAGE")

    assert calls == 2
    assert em.provider_health()["state"] == "open"


def test_circuit_breakers_are_isolated_per_report(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        em,
        "_settings",
        lambda: {
            "timeout_seconds": 1,
            "max_attempts": 1,
            "minimum_interval_seconds": 0,
            "jitter_max_seconds": 0,
            "backoff_base_seconds": 0.01,
            "circuit_failure_threshold": 2,
            "circuit_open_seconds": 60,
            "coordination_backend": "shared_file",
            "coordination_timeout_seconds": 1,
            "coordination_stale_seconds": 10,
        },
    )
    monkeypatch.setattr(em, "_wait_for_provider_slot", lambda *args, **kwargs: None)
    calls = []

    def request(request, **kwargs):
        calls.append(request.full_url)
        if "RPT_FAILING" in request.full_url:
            raise DataSourceError(
                "eastmoney",
                "HTTP 503",
                error_type=ErrorType.HTTP,
                status_code=503,
            )
        return HttpResult(
            {"success": True, "code": 0, "result": {"data": []}},
            "2026-06-15T08:00:00+00:00",
            1,
        )

    monkeypatch.setattr(em, "request_json", request)

    for _ in range(2):
        with pytest.raises(DataSourceError):
            em.datacenter_query("RPT_FAILING")
    assert em.datacenter_query("RPT_HEALTHY") == []
    with pytest.raises(DataSourceError, match="circuit"):
        em.datacenter_query("RPT_FAILING")

    health = em.provider_health()
    assert health["circuits"]["datacenter:RPT_FAILING"]["state"] == "open"
    assert health["circuits"]["datacenter:RPT_HEALTHY"]["state"] == "closed"
    assert len(calls) == 3


def test_report_schema_drift_is_a_typed_failure(monkeypatch):
    monkeypatch.setattr(em, "_wait_for_provider_slot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        em,
        "request_json",
        lambda *args, **kwargs: HttpResult(
            {"success": True, "code": 0, "payload": []},
            "2026-06-15T08:00:00+00:00",
            1,
        ),
    )

    with pytest.raises(DataSourceError) as caught:
        em.fetch_reports("002156")

    assert caught.value.error_type == ErrorType.INVALID_RESPONSE


def test_shared_coordination_lock_serializes_competing_workers():
    events = []
    first_entered = threading.Event()

    def first():
        with em._coordination_lock("concurrency", timeout=1, stale_after=10):
            events.append("first_enter")
            first_entered.set()
            time.sleep(0.1)
            events.append("first_exit")

    def second():
        first_entered.wait(timeout=1)
        with em._coordination_lock("concurrency", timeout=1, stale_after=10):
            events.append("second_enter")

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert events == ["first_enter", "first_exit", "second_enter"]


def test_shared_coordination_lock_recovers_stale_owner():
    lock_dir = os.path.join(em._coordination_dir(), "stale-test.lock")
    os.makedirs(lock_dir, exist_ok=True)
    with open(os.path.join(lock_dir, "owner.json"), "w", encoding="utf-8") as handle:
        json.dump({"token": "dead", "created_epoch": 1}, handle)

    with em._coordination_lock("stale-test", timeout=1, stale_after=0.01):
        assert os.path.isdir(lock_dir)

    assert not os.path.exists(lock_dir)
