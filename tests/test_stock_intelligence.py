from datetime import date

import decision_policy
import research_evidence
import stock_intelligence


def _payload():
    return {
        "schema": stock_intelligence.SCHEMA,
        "code": "002156",
        "asof": "2026-06-15",
        "fetched_at": "2026-06-15T08:00:00+00:00",
        "lockups": {
            "upcoming": [{
                "date": "2026-06-20",
                "type": "定向增发机构配售股份",
                "shares": 12000000,
                "ratio_pct": 12.5,
            }],
        },
        "margin_trading": [
            {"date": "2026-06-13", "financing_balance": 120.0},
            {"date": "2026-06-06", "financing_balance": 100.0},
        ],
        "holder_changes": [
            {"date": "2026-03-31", "holder_change_pct": 8.0},
            {"date": "2025-12-31", "holder_change_pct": 5.0},
        ],
        "dragon_tiger": {
            "records": [{"date": "2026-06-13"}],
            "institution": {"net_amount_wan": -6000.0},
        },
        "block_trades": [],
        "reports": [],
        "dataset_status": {
            "lockups": {
                "status": "ok",
                "queried_asof": "2026-06-15",
                "latest_record_date": "2026-06-20",
            },
            "margin_trading": {
                "status": "ok",
                "queried_asof": "2026-06-15",
                "latest_record_date": "2026-06-13",
            },
            "holder_changes": {
                "status": "ok",
                "queried_asof": "2026-06-15",
                "latest_record_date": "2026-03-31",
            },
            "dragon_tiger": {
                "status": "ok",
                "queried_asof": "2026-06-15",
                "latest_record_date": "2026-06-13",
            },
            "block_trades": {
                "status": "empty",
                "queried_asof": "2026-06-15",
                "latest_record_date": None,
            },
            "reports": {
                "status": "empty",
                "queried_asof": "2026-06-15",
                "latest_record_date": None,
            },
        },
        "data_quality": {
            "status": "complete",
            "missing_datasets": [],
            "stale_datasets": [],
            "directional_ready": True,
            "errors": [],
        },
    }


def test_risk_summary_flags_major_lockup_and_crowding():
    summary = stock_intelligence.assess_risks(_payload(), asof=date(2026, 6, 15))

    assert "major_lockup_within_30d" in summary["hard_risks"]
    assert "financing_balance_surge" in summary["warnings"]
    assert "holder_count_rising" in summary["warnings"]
    assert "institutional_lhb_net_sell" in summary["warnings"]


def test_cache_round_trip_enters_research_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    payload = _payload()
    payload["risk_summary"] = stock_intelligence.assess_risks(payload)
    stock_intelligence.write_cache(payload)

    monkeypatch.setattr(research_evidence, "_chanlun_evidence", lambda strategy_id: {})
    monkeypatch.setattr(research_evidence, "_serenity_evidence", lambda code, asof: {
        "available": False,
        "stale": None,
        "hard_risks": [],
    })
    evidence = research_evidence.build_research_evidence(
        "002156",
        strategy_id="daban:first_board_reseal",
        asof="2026-06-15",
    )

    assert evidence["market_intelligence"]["available"] is True
    assert "major_lockup_within_30d" in evidence["market_intelligence"]["hard_risks"]

    decision = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        research_evidence=evidence,
        strategy_lane="daban",
    )
    assert decision["decision"] == "avoid"
    assert "market_intelligence_hard_risk" in decision["reasons"]


def test_stale_cache_is_disclosed_but_not_used_as_hard_veto(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    payload = _payload()
    payload["asof"] = "2026-05-01"
    payload["risk_summary"] = stock_intelligence.assess_risks(payload)
    stock_intelligence.write_cache(payload)

    cached = stock_intelligence.read_cache(
        "002156",
        asof=date(2026, 6, 15),
        max_age_days=7,
    )

    assert cached["stale"] is True
    assert cached["hard_risks"] == []
    assert "stale_market_intelligence" in cached["warnings"]


def test_collect_discloses_partial_provider_failures(monkeypatch):
    monkeypatch.setattr(
        stock_intelligence,
        "fetch_lockups",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("blocked")),
    )
    monkeypatch.setattr(stock_intelligence, "fetch_margin_trading", lambda code: [])
    monkeypatch.setattr(stock_intelligence, "fetch_holder_changes", lambda code: [])
    monkeypatch.setattr(
        stock_intelligence,
        "fetch_dragon_tiger",
        lambda code, asof=None: {
            "records": [],
            "seats": {"buy": [], "sell": []},
            "institution": {"net_amount_wan": 0},
        },
    )
    monkeypatch.setattr(stock_intelligence, "fetch_block_trades", lambda code: [])
    monkeypatch.setattr(stock_intelligence, "fetch_reports", lambda code: [])
    monkeypatch.setattr(
        stock_intelligence,
        "fetch_interactive_qa",
        lambda code, asof=None, retention=10: {
            "market": "szse", "status": "empty", "rows": [],
        },
    )

    payload = stock_intelligence.collect("002156", asof="2026-06-15")

    assert payload["data_quality"]["status"] == "partial"
    assert payload["data_quality"]["missing_datasets"] == ["lockups"]
    assert payload["data_quality"]["directional_ready"] is False
    assert payload["dataset_status"]["lockups"]["status"] == "error"
    assert payload["lockups"] == {"history": [], "upcoming": []}


def test_missing_cache_is_explicitly_not_directional_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    cached = stock_intelligence.read_cache("002156", asof="2026-06-15")

    assert cached["available"] is False
    assert cached["directional_ready"] is False
    assert set(cached["missing_datasets"]) == set(
        stock_intelligence.REQUIRED_DATASETS
    )


def test_stale_required_dataset_blocks_directional_readiness(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    payload = _payload()
    payload["dataset_status"]["margin_trading"]["latest_record_date"] = "2026-05-01"
    payload["risk_summary"] = stock_intelligence.assess_risks(payload)
    stock_intelligence.write_cache(payload)

    cached = stock_intelligence.read_cache("002156", asof="2026-06-15")

    assert cached["available"] is True
    assert cached["directional_ready"] is False
    assert "margin_trading" in cached["stale_datasets"]
    assert "major_lockup_within_30d" in cached["hard_risks"]


def test_legacy_schema_is_visible_but_never_directional(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    payload = _payload()
    payload["schema"] = "stock_intelligence_v1"
    from state_store import atomic_write_json

    atomic_write_json(stock_intelligence.cache_file("002156"), payload)

    cached = stock_intelligence.read_cache("002156", asof="2026-06-15")

    assert cached["available"] is True
    assert cached["directional_ready"] is False
    assert "legacy_market_intelligence_schema" in cached["warnings"]


def test_partial_refresh_uses_fresh_last_known_good_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    complete = _payload()
    complete["risk_summary"] = stock_intelligence.assess_risks(complete)
    stock_intelligence.write_cache(complete)

    partial = _payload()
    partial["dataset_status"]["lockups"]["status"] = "error"
    partial["dataset_status"]["lockups"]["error"] = {
        "dataset": "lockups",
        "error": "provider unavailable",
        "error_type": "DataSourceError",
    }
    partial["data_quality"] = {
        "status": "partial",
        "missing_datasets": ["lockups"],
        "stale_datasets": [],
        "missing_required_datasets": ["lockups"],
        "stale_required_datasets": [],
        "directional_ready": False,
        "errors": [partial["dataset_status"]["lockups"]["error"]],
    }
    partial["lockups"] = {"history": [], "upcoming": []}
    partial["risk_summary"] = stock_intelligence.assess_risks(partial)
    stock_intelligence.write_cache(partial)

    cached = stock_intelligence.read_cache("002156", asof="2026-06-15")

    assert cached["directional_ready"] is True
    assert cached["fallback_used"] is True
    assert "using_last_known_good_market_intelligence" in cached["warnings"]


def test_fresh_lockup_hard_risk_survives_unrelated_stale_dataset(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    payload = _payload()
    payload["dataset_status"]["holder_changes"]["latest_record_date"] = "2025-01-01"
    payload["risk_summary"] = stock_intelligence.assess_risks(payload)
    stock_intelligence.write_cache(payload)

    cached = stock_intelligence.read_cache("002156", asof="2026-06-15")

    assert cached["directional_ready"] is False
    assert "holder_changes" in cached["stale_datasets"]
    assert "major_lockup_within_30d" in cached["hard_risks"]
    assert cached["fallback_used"] is False


def _stub_required_fetches(monkeypatch):
    monkeypatch.setattr(stock_intelligence, "fetch_lockups", lambda *a, **k: {"history": [], "upcoming": []})
    monkeypatch.setattr(stock_intelligence, "fetch_margin_trading", lambda code: [])
    monkeypatch.setattr(stock_intelligence, "fetch_holder_changes", lambda code: [])
    monkeypatch.setattr(
        stock_intelligence,
        "fetch_dragon_tiger",
        lambda code, asof=None: {
            "records": [], "seats": {"buy": [], "sell": []},
            "institution": {"net_amount_wan": 0},
        },
    )
    monkeypatch.setattr(stock_intelligence, "fetch_block_trades", lambda code: [])
    monkeypatch.setattr(stock_intelligence, "fetch_reports", lambda code: [])


def test_collect_enters_szse_interactive_qa_rows_into_cache(monkeypatch):
    _stub_required_fetches(monkeypatch)
    rows = [{
        "date": "2026-06-10",
        "question_date": "2026-06-09",
        "reply_date": "2026-06-10",
        "question": "公司订单情况如何？",
        "reply": "在手订单饱满。",
        "has_reply": True,
        "platform": "szse_irm",
        "company": "通富微电",
        "url": "https://irm.cninfo.com.cn/mobile/rmDetail?questionId=1",
    }]
    monkeypatch.setattr(
        stock_intelligence,
        "fetch_interactive_qa",
        lambda code, asof=None, retention=10: {
            "market": "szse", "status": "ok", "rows": rows,
        },
    )

    payload = stock_intelligence.collect("002156", asof="2026-06-15")

    assert payload["interactive_qa"]["status"] == "ok"
    assert payload["interactive_qa"]["rows"] == rows
    assert payload["dataset_status"]["interactive_qa"]["status"] == "ok"
    assert payload["dataset_status"]["interactive_qa"]["provider"] == "cninfo_sse"
    assert payload["dataset_status"]["interactive_qa"]["required"] is False
    assert payload["dataset_status"]["interactive_qa"]["latest_record_date"] == "2026-06-10"


def test_collect_fails_closed_when_szse_interactive_qa_errors(monkeypatch):
    _stub_required_fetches(monkeypatch)

    def fail(*args, **kwargs):
        raise RuntimeError("irm unreachable")

    monkeypatch.setattr(stock_intelligence, "fetch_interactive_qa", fail)

    payload = stock_intelligence.collect("002156", asof="2026-06-15")

    assert payload["interactive_qa"] == {"market": None, "status": "empty", "rows": []}
    assert payload["dataset_status"]["interactive_qa"]["status"] == "error"
    assert "interactive_qa" in payload["data_quality"]["missing_datasets"]
    # optional dataset: a failure never blocks directional readiness.
    assert "interactive_qa" not in payload["data_quality"]["missing_required_datasets"]
    assert payload["data_quality"]["directional_ready"] is True


def test_collect_marks_sse_unavailable_without_recording_hard_error(monkeypatch):
    _stub_required_fetches(monkeypatch)
    monkeypatch.setattr(
        stock_intelligence,
        "fetch_interactive_qa",
        lambda code, asof=None, retention=10: {
            "market": "sse",
            "status": "sse_unavailable",
            "rows": [],
            "error": {"source": "sse", "error": "uid not found"},
        },
    )

    payload = stock_intelligence.collect("600519", asof="2026-06-15")

    assert payload["interactive_qa"]["status"] == "sse_unavailable"
    assert payload["dataset_status"]["interactive_qa"]["status"] == "sse_unavailable"
    # sse_unavailable is a documented best-effort degrade, not a provider
    # error entry in data_quality.errors.
    assert payload["data_quality"]["errors"] == []
    assert "interactive_qa" in payload["data_quality"]["missing_datasets"]


def test_collect_retention_forwarded_to_interactive_qa_fetch(monkeypatch):
    _stub_required_fetches(monkeypatch)
    captured = {}

    def fake_fetch(code, asof=None, retention=10):
        captured["retention"] = retention
        return {"market": "szse", "status": "empty", "rows": []}

    monkeypatch.setattr(stock_intelligence, "fetch_interactive_qa", fake_fetch)

    stock_intelligence.collect("002156", asof="2026-06-15", interactive_qa_retention=3)

    assert captured["retention"] == 3


def test_read_interactive_qa_returns_missing_when_cache_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    result = stock_intelligence.read_interactive_qa("002156")

    assert result == {"available": False, "status": "missing", "market": None, "rows": []}


def test_read_interactive_qa_returns_rows_from_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    payload = _payload()
    rows = [
        {"date": f"2026-06-{10 + i:02d}", "question": f"q{i}", "reply": f"a{i}", "has_reply": True}
        for i in range(15)
    ]
    payload["interactive_qa"] = {"market": "szse", "status": "ok", "rows": rows}
    payload["dataset_status"]["interactive_qa"] = {
        "provider": "cninfo_sse", "status": "ok", "required": False,
        "queried_asof": "2026-06-15", "latest_record_date": "2026-06-24",
        "max_query_age_days": 7, "max_record_age_days": 180, "error": None,
    }
    payload["risk_summary"] = stock_intelligence.assess_risks(payload)
    stock_intelligence.write_cache(payload)

    result = stock_intelligence.read_interactive_qa("002156", retention=10)

    assert result["available"] is True
    assert result["status"] == "ok"
    assert result["market"] == "szse"
    assert len(result["rows"]) == 10


def test_read_interactive_qa_falls_back_to_last_good_when_live_missing_section(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    good = _payload()
    good["interactive_qa"] = {
        "market": "szse", "status": "ok",
        "rows": [{"date": "2026-06-10", "question": "q", "reply": "a", "has_reply": True}],
    }
    good["risk_summary"] = stock_intelligence.assess_risks(good)
    stock_intelligence.write_cache(good)

    live = _payload()
    live["asof"] = "2026-06-16"
    live.pop("interactive_qa", None)
    live["risk_summary"] = stock_intelligence.assess_risks(live)
    from state_store import atomic_write_json
    atomic_write_json(stock_intelligence.cache_file("002156"), live)

    result = stock_intelligence.read_interactive_qa("002156")

    assert result["available"] is True
    assert len(result["rows"]) == 1
