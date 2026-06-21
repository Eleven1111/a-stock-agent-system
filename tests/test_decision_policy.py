import decision_policy


ALLOWED_STRATEGY = {
    "allowed_in_live_agent": True,
    "gating_status": "enabled",
    "runtime_allowed": True,
}


def test_unregistered_strategy_is_research_only():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=None,
    )

    assert result["decision"] == "watch"
    assert result["position_multiplier"] == 0.0
    assert "strategy_unverified" in result["reasons"]


def test_self_declared_allowed_strategy_without_runtime_verification_is_blocked():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record={
            "allowed_in_live_agent": True,
            "gating_status": "enabled",
        },
    )

    assert result["decision"] == "avoid"
    assert result["position_multiplier"] == 0.0
    assert "strategy_not_allowed" in result["reasons"]


def test_unregistered_strategy_cannot_downgrade_quality_rejection_to_watch():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "rejected"},
        strategy_record=None,
    )

    assert result["decision"] == "avoid"
    assert result["position_multiplier"] == 0.0
    assert "quality_rejected" in result["reasons"]


def test_disabled_strategy_cannot_emit_buy():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record={
            "allowed_in_live_agent": True,
            "gating_status": "disabled",
            "runtime_allowed": False,
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


def test_serenity_hard_risk_blocks_positive_action():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        research_evidence={
            "serenity": {
                "available": True,
                "hard_risks": ["risk_control=1/5"],
            }
        },
    )

    assert result["decision"] == "avoid"
    assert "serenity_hard_risk" in result["reasons"]


def test_portfolio_concentration_blocks_positive_action():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        portfolio_risk={
            "allowed": False,
            "reasons": ["single_position_limit"],
        },
    )

    assert result["decision"] == "avoid"
    assert "single_position_limit" in result["reasons"]


def test_market_intelligence_not_ready_blocks_positive_action():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        research_evidence={
            "market_intelligence": {
                "available": True,
                "directional_ready": False,
                "hard_risks": [],
            }
        },
    )

    assert result["decision"] == "watch"
    assert result["position_multiplier"] == 0.0
    assert "market_intelligence_not_ready" in result["reasons"]


def test_stale_serenity_reduces_trend_position_without_blocking_daban():
    evidence = {
        "serenity": {
            "available": True,
            "stale": True,
            "hard_risks": [],
        }
    }

    trend = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        research_evidence=evidence,
        strategy_lane="trend",
    )
    daban = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        research_evidence=evidence,
        strategy_lane="daban",
    )

    assert trend["decision"] == "buy"
    assert trend["position_multiplier"] == 0.5
    assert daban["position_multiplier"] == 1.0
