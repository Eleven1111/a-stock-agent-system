import decision_policy


def test_disabled_strategy_cannot_emit_buy():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record={
            "allowed_in_live_agent": True,
            "gating_status": "disabled",
        },
    )

    assert result["decision"] == "avoid"
    assert result["position_multiplier"] == 0.0


def test_conditional_quality_downgrades_buy_to_watch():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "conditional"},
        strategy_record=None,
    )

    assert result["decision"] == "watch"
