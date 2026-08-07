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


def test_unknown_market_context_blocks_positive_action():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_regime={"regime": "unknown", "reason": "market context missing"},
    )

    assert result["decision"] == "watch"
    assert result["position_multiplier"] == 0.0
    assert "market_context_unknown" in result["reasons"]


def test_stale_market_context_blocks_positive_action():
    result = decision_policy.evaluate_decision(
        requested_action="add",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_regime={"regime": "stale", "reason": "market context expired"},
    )

    assert result["decision"] == "watch"
    assert result["position_multiplier"] == 0.0
    assert "market_context_stale" in result["reasons"]


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


def test_reflexivity_leader_isolation_blocks_new_daban_position():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        strategy_lane="daban",
        market_crowding={
            "reflexivity": {
                "status": "ready",
                "phase": "distribution",
                "defensive_guards": ["leader_isolation_exit_v1"],
                "risk_multiplier": 0.0,
            }
        },
    )

    assert result["decision"] == "watch"
    assert result["position_multiplier"] == 0.0
    assert "reflexivity_leader_isolation" in result["reasons"]


def test_institution_distribution_plus_retail_crowding_blocks_chasing():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        research_evidence={
            "market_intelligence": {
                "available": True,
                "directional_ready": True,
                "hard_risks": [],
                "warnings": ["institutional_lhb_net_sell"],
            }
        },
        market_crowding={"crowding_score": 0.75},
    )

    assert result["decision"] == "avoid"
    assert result["position_multiplier"] == 0.0
    assert "reflexivity_institution_distribution" in result["reasons"]


def test_reflexivity_positive_phase_never_bypasses_strategy_registry():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=None,
        market_crowding={
            "reflexivity": {
                "status": "ready",
                "phase": "diffusion",
                "positive_admission": False,
                "risk_multiplier": 1.0,
            }
        },
    )

    assert result["decision"] == "watch"
    assert result["position_multiplier"] == 0.0
    assert "strategy_unverified" in result["reasons"]


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


def test_discipline_freeze_blocks_daban_lane():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        strategy_lane="daban",
        discipline_state={"blocked": True, "reasons": ["day_loss_stop"]},
    )

    assert result["decision"] == "avoid"
    assert result["position_multiplier"] == 0.0
    assert "day_loss_stop" in result["reasons"]


def test_discipline_freeze_does_not_apply_to_trend_lane():
    # market_gate 阈值(周3笔/日跌2%等)是打板专属节奏，不应误伤趋势策略
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        strategy_lane="trend",
        discipline_state={"blocked": True, "reasons": ["day_loss_stop"]},
    )

    assert result["decision"] == "buy"
    assert "day_loss_stop" not in result["reasons"]


# --- guardrail: structured raw_action vs final_action divergence explanation ---


def test_guardrail_present_when_raw_action_diverges_from_final_decision():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        portfolio_risk={"allowed": False, "reasons": ["single_position_limit"]},
    )

    assert result["decision"] == "avoid"
    guardrail = result["guardrail"]
    assert guardrail["raw_action"] == "buy"
    assert guardrail["final_action"] == "avoid"
    codes = [r["code"] for r in guardrail["reasons"]]
    assert "concentration" in codes
    concentration = next(r for r in guardrail["reasons"] if r["code"] == "concentration")
    assert "single_position_limit" in concentration["detail"]


def test_guardrail_absent_when_action_matches_decision():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
    )

    assert result["decision"] == "buy"
    assert result["guardrail"] is None


def test_guardrail_classifies_announcement_and_quality_reasons():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "rejected"},
    )

    guardrail = result["guardrail"]
    assert guardrail is not None
    codes = {r["code"] for r in guardrail["reasons"]}
    assert "strategy_gate" in codes


def test_guardrail_classifies_temperature_and_market_state_reasons():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        market_regime={"regime": "risk_off"},
    )

    guardrail = result["guardrail"]
    assert guardrail is not None
    codes = {r["code"] for r in guardrail["reasons"]}
    assert "temperature_gate" in codes


def test_guardrail_classifies_discipline_reasons():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        strategy_lane="daban",
        discipline_state={"blocked": True, "reasons": ["day_loss_stop"]},
    )

    guardrail = result["guardrail"]
    assert guardrail is not None
    codes = {r["code"] for r in guardrail["reasons"]}
    assert "discipline_gate" in codes


def test_guardrail_classifies_t1_lock():
    result = decision_policy.evaluate_decision(
        requested_action="sell",
        quality_report={"status": "passed"},
        t1_block={"error": "A股T+1限制：当日买入/加仓股份不能当日卖出", "code": "T1_LOCKED"},
    )

    assert result["decision"] == "hold_locked"
    guardrail = result["guardrail"]
    assert guardrail is not None
    assert guardrail["raw_action"] == "sell"
    assert guardrail["final_action"] == "hold_locked"
    codes = {r["code"] for r in guardrail["reasons"]}
    assert "t_plus_1" in codes


def test_guardrail_includes_raw_score_when_provided():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        portfolio_risk={"allowed": False, "reasons": ["sector_exposure_limit"]},
        raw_score=87.5,
    )

    assert result["guardrail"]["raw_score"] == 87.5


def test_guardrail_unknown_reason_falls_back_to_other_code():
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        portfolio_risk={"allowed": False, "reasons": ["some_new_未知原因"]},
    )

    guardrail = result["guardrail"]
    assert guardrail is not None
    codes = {r["code"] for r in guardrail["reasons"]}
    assert "other" in codes


def test_missing_serenity_evidence_is_not_treated_as_passing():
    """证据缺失不得与「检查通过」等价。

    _serenity_evidence 在没有深研记录时返回 available=False、hard_risks=[]，
    于是 hard_risks 分支与 stale 分支都不触发，正向动作原样放行——
    serenity_hard_risk 这道一票否决在没有深研缓存的标的上是个哑弹。
    对照组是紧邻的 market_intelligence：它有显式的 not available → watch 分支。
    """
    evidence = {"serenity": {"available": False, "stale": None, "hard_risks": []}}

    trend = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        research_evidence=evidence,
        strategy_lane="trend",
    )

    assert trend["decision"] == "watch"
    assert trend["position_multiplier"] == 0.0
    assert "serenity_evidence_missing" in trend["reasons"]


def test_missing_serenity_evidence_does_not_gate_daban_lane():
    """打板吃的是 T+1 情绪溢价，结构上不可能有全市场深研覆盖；
    stale 规则本来就只作用于 trend lane，缺失规则同样不得扩到 daban。"""
    daban = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        research_evidence={"serenity": {"available": False, "hard_risks": []}},
        strategy_lane="daban",
    )

    assert daban["decision"] == "buy"
    assert daban["position_multiplier"] == 1.0
    assert "serenity_evidence_missing" not in daban["reasons"]


def test_callers_without_serenity_key_are_unaffected():
    """未接线 serenity 的调用方不应被这条 fail-closed 规则误伤（与
    market_intelligence 的 'key 在才判定' 语义保持一致）。"""
    result = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=ALLOWED_STRATEGY,
        research_evidence={"chanlun": {}},
        strategy_lane="trend",
    )

    assert result["decision"] == "buy"
    assert "serenity_evidence_missing" not in result["reasons"]


def test_missing_serenity_reason_is_grouped_not_other():
    assert decision_policy._guardrail_reason_code(
        "serenity_evidence_missing"
    ) != "other"
