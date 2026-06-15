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
