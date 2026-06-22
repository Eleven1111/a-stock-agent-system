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


def test_crowding_climax_only_observed_by_default(monkeypatch):
    # 默认 observe：高拥挤+高脆弱只记录，不降级（横截面预警是非确定性证据）
    monkeypatch.delenv("HERMES_CROWDING_GUARD", raising=False)
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_crowding={"crowding_score": 0.7, "fragility_score": 0.6},
    )

    assert result["decision"] == "buy"
    assert result["position_multiplier"] == 1.0
    assert "crowding_climax_observed" in result["reasons"]


def test_crowding_climax_reduces_position_when_enforced(monkeypatch):
    monkeypatch.setenv("HERMES_CROWDING_GUARD", "enforce")
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_crowding={"crowding_score": 0.7, "fragility_score": 0.6},
    )

    assert result["decision"] == "buy"
    assert result["position_multiplier"] == 0.5
    assert "crowding_climax_reduced" in result["reasons"]


def test_crowding_guard_fails_open_when_scores_missing(monkeypatch):
    # 数据不足 → 不干预（避免缺数据误杀正常建议）
    monkeypatch.setenv("HERMES_CROWDING_GUARD", "enforce")
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_crowding={"status": "insufficient_data", "crowding_score": None, "fragility_score": None},
    )

    assert result["position_multiplier"] == 1.0
    assert not any("crowding_climax" in reason for reason in result["reasons"])


def test_crowding_guard_skips_below_threshold(monkeypatch):
    monkeypatch.setenv("HERMES_CROWDING_GUARD", "enforce")
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_crowding={"crowding_score": 0.5, "fragility_score": 0.4},
    )

    assert result["position_multiplier"] == 1.0
    assert not any("crowding_climax" in reason for reason in result["reasons"])


def test_ebbing_state_reduces_position_when_enforced(monkeypatch):
    # S6 退潮态 → 与高潮拥挤同等温和降级（含 trend lane）
    monkeypatch.setenv("HERMES_CROWDING_GUARD", "enforce")
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_crowding={"dominant_state": "S6"},
    )

    assert result["position_multiplier"] == 0.5
    assert "market_state_ebbing_reduced" in result["reasons"]


def test_ebbing_state_only_observed_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_CROWDING_GUARD", raising=False)
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_crowding={"dominant_state": "S6"},
    )

    assert result["position_multiplier"] == 1.0
    assert "market_state_ebbing_observed" in result["reasons"]
