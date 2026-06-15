from datetime import date

import decision_policy
import research_evidence
import stock_intelligence


def _payload():
    return {
        "schema": "stock_intelligence_v1",
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
    assert payload["lockups"] == {"history": [], "upcoming": []}
