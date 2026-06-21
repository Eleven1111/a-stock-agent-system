import deep_research_cache
import research_evidence
import strategy_registry


def test_research_evidence_combines_chanlun_gate_and_serenity_risk(
    tmp_path,
    monkeypatch,
    verified_gate_factory,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    strategy_registry.register_gate_result(
        "chanlun_third_buy",
        verified_gate_factory("chanlun_third_buy"),
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


def test_live_chanlun_signals_become_directional_strategy_attributions():
    evidence = {
        "chanlun": {
            "live_bullish_signals": [
                {
                    "type": "third_buy",
                    "strategy_id": "chanlun_third_buy",
                    "idx": 58,
                }
            ],
            "live_bearish_signals": [
                {
                    "type": "top_divergence",
                    "strategy_id": "chanlun_top_divergence",
                    "idx": 59,
                }
            ],
        }
    }

    attributions = research_evidence.strategy_attributions(evidence)

    assert attributions == [
        {
            "strategy_id": "chanlun_third_buy",
            "role": "research_evidence",
            "direction": "bullish",
            "signal_type": "third_buy",
            "signal_idx": 58,
        },
        {
            "strategy_id": "chanlun_top_divergence",
            "role": "research_evidence",
            "direction": "bearish",
            "signal_type": "top_divergence",
            "signal_idx": 59,
        },
    ]
