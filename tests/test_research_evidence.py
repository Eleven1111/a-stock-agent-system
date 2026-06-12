import deep_research_cache
import research_evidence
import strategy_registry


def test_research_evidence_combines_chanlun_gate_and_serenity_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    strategy_registry.register_gate_result(
        "chanlun_third_buy",
        {
            "decision": "pass",
            "allowed_in_live_agent": True,
            "asof": "2026-06-10",
            "stats": {"trades": 100},
        },
    )
    deep_research_cache.write_deep_research(
        "600001",
        "测试股票",
        {
            "total": 58,
            "rating": "谨慎",
            "dimensions": {
                "financial_quality": {"score_1_to_5": 2},
                "risk_control": {"score_1_to_5": 1},
            },
        },
        asof="2026-06-10",
    )

    evidence = research_evidence.build_research_evidence(
        "600001",
        strategy_id="chanlun_third_buy",
        asof="2026-06-12",
    )

    assert evidence["chanlun"]["status"] == "live_allowed"
    assert evidence["serenity"]["available"] is True
    assert "risk_control=1/5" in evidence["serenity"]["hard_risks"]
