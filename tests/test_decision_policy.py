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
    # S6 退潮态是硬风险态，始终降到报告建议的 0-20% 暴露（含 trend lane）
    monkeypatch.setenv("HERMES_CROWDING_GUARD", "enforce")
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_crowding={"dominant_state": "S6"},
    )

    assert result["position_multiplier"] == 0.2
    assert "market_state_ebbing_reduced" in result["reasons"]


def test_ebbing_state_reduces_risk_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_CROWDING_GUARD", raising=False)
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_crowding={"dominant_state": "S6"},
    )

    assert result["position_multiplier"] == 0.2
    assert "market_state_ebbing_reduced" in result["reasons"]


def test_expected_paths_collapse_dominates_in_ebbing_state():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_crowding={"dominant_state": "S6"},
    )
    paths = {p["scenario"]: p["prob"] for p in result["expected_paths"]}
    assert result["expected_paths_calibrated"] is False
    assert paths["collapse"] == max(paths.values())
    assert abs(sum(paths.values()) - 1.0) < 1e-3


def test_expected_paths_continue_dominates_in_expansion():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_crowding={"dominant_state": "S2"},
    )
    paths = {p["scenario"]: p["prob"] for p in result["expected_paths"]}
    assert paths["continue"] == max(paths.values())


def test_expected_paths_none_without_market_state():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
    )
    assert result["expected_paths"] is None


def test_fragility_shifts_paths_toward_collapse():
    base = decision_policy.evaluate_decision(
        requested_action="buy", quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY, market_crowding={"dominant_state": "S4"},
    )
    fragile = decision_policy.evaluate_decision(
        requested_action="buy", quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_crowding={"dominant_state": "S4", "fragility_score": 0.8},
    )
    base_coll = next(p["prob"] for p in base["expected_paths"] if p["scenario"] == "collapse")
    fragile_coll = next(p["prob"] for p in fragile["expected_paths"] if p["scenario"] == "collapse")
    assert fragile_coll > base_coll


def test_abstain_marks_non_action_watch_but_not_executable_buy():
    # 未注册策略 → watch → abstain(无优势主动弃权, 报告"ABSTAIN 是完整正确输出")
    watched = decision_policy.evaluate_decision(
        requested_action="buy", quality_report={"status": "passed"}, strategy_record=None,
    )
    assert watched["decision"] == "watch" and watched["abstain"] is True
    # 可执行 buy → 非 abstain
    executable = decision_policy.evaluate_decision(
        requested_action="buy", quality_report={"status": "passed"}, strategy_record=ALLOWED_STRATEGY,
    )
    assert executable["decision"] == "buy" and executable["abstain"] is False
