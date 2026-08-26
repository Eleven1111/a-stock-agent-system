"""P4(c) 环境总仓表接进 decision_policy 的行为断言 — 升级方案 §7.2。

两组断言：
1. **未启用时逐字段一致** —— 全表接管的是实盘链路的仓位倍率，默认关闭必须证明
   输出与改造前一字不差，而不是「看起来差不多」。
2. **启用后的行为断言** —— 构造退潮/高潮/发酵日 mock，验证**正向建议实际输出的
   仓位**落在表内区间，而不是去读配置字段的值（读字段只能证明配置写对了，
   证明不了消费端真的读了它）。
"""

import copy

import pytest

import decision_policy
import position_risk

ALLOWED_STRATEGY = {
    "allowed_in_live_agent": True,
    "gating_status": "enabled",
    "runtime_allowed": True,
}

ALL_STATES = list(position_risk.ENVIRONMENT_POSITION_TABLE)


def _evaluate(state, **overrides):
    kwargs = {
        "requested_action": "buy",
        "quality_report": {"status": "passed"},
        "strategy_record": ALLOWED_STRATEGY,
        "market_crowding": {"dominant_state": state},
    }
    kwargs.update(overrides)
    return decision_policy.evaluate_decision(**kwargs)


# ── 1. 默认关闭：逐字段一致 ────────────────────────────────────────────────

@pytest.mark.parametrize("state", ALL_STATES + [None, "S9"])
def test_output_is_field_for_field_identical_when_flag_absent(state, monkeypatch):
    monkeypatch.delenv("HERMES_ENV_POSITION_TABLE", raising=False)
    monkeypatch.delenv("HERMES_CROWDING_GUARD", raising=False)
    result = _evaluate(state)

    # 改造前的口径：只有 S6 被压到 0.2，其余状态倍率 1.0，且不出现任何
    # env_position_table_* 理由。
    expected_multiplier = 0.2 if state == "S6" else 1.0
    expected_reasons = ["market_state_ebbing_reduced"] if state == "S6" else []
    assert result["position_multiplier"] == pytest.approx(expected_multiplier)
    assert result["reasons"] == expected_reasons
    assert result["decision"] == "buy"
    assert not any(r.startswith("env_position_table") for r in result["reasons"])


@pytest.mark.parametrize("value", ["off", "observe", "", "true", "1"])
def test_only_the_literal_enforce_turns_the_table_on(value, monkeypatch):
    """半开状态是最坏的：任何非 ``enforce`` 的值都保持旧行为。"""
    monkeypatch.setenv("HERMES_ENV_POSITION_TABLE", value)
    result = _evaluate("S2")
    assert result["position_multiplier"] == pytest.approx(1.0)
    assert result["reasons"] == []


def test_full_payload_matches_the_flag_off_snapshot(monkeypatch):
    """整个返回体（不只是倍率）在开关关闭时与基线快照逐字段相同。"""
    monkeypatch.delenv("HERMES_ENV_POSITION_TABLE", raising=False)
    baseline = copy.deepcopy(_evaluate("S6"))
    monkeypatch.setenv("HERMES_ENV_POSITION_TABLE", "off")
    assert _evaluate("S6") == baseline


# ── 2. 启用后：正向建议的实际输出落在表内区间 ──────────────────────────────

@pytest.mark.parametrize("state", ALL_STATES)
def test_actual_position_output_lands_inside_the_table_band(state, monkeypatch):
    monkeypatch.setenv("HERMES_ENV_POSITION_TABLE", "enforce")
    monkeypatch.delenv("HERMES_CROWDING_GUARD", raising=False)
    result = _evaluate(state)

    band = position_risk.ENVIRONMENT_POSITION_TABLE[state]
    # 满仓基准 100% × 倍率 = 实际建议总仓；必须 ≤ 该状态的上限。
    actual_pct = result["position_multiplier"] * 100.0
    assert actual_pct <= band["max_pct"] + 1e-9, (state, actual_pct, band)
    assert f"env_position_table_{state}" in result["reasons"]


def test_ebbing_day_mock_caps_positive_advice_at_10_pct(monkeypatch):
    """退潮日（S6）：正向建议实际输出 ≤ 10%，比旧规则的 20% 更紧。"""
    monkeypatch.setenv("HERMES_ENV_POSITION_TABLE", "enforce")
    result = _evaluate("S6")
    assert result["decision"] == "buy"
    assert result["position_multiplier"] * 100.0 <= 10.0
    assert "market_state_ebbing_reduced" not in result["reasons"]  # 被全表取代


def test_climax_day_mock_caps_positive_advice_at_30_pct(monkeypatch):
    """高潮日（S4）：旧口径完全不降仓，全表把它压到 30%。"""
    monkeypatch.setenv("HERMES_ENV_POSITION_TABLE", "enforce")
    result = _evaluate("S4")
    assert result["position_multiplier"] * 100.0 <= 30.0

    monkeypatch.delenv("HERMES_ENV_POSITION_TABLE", raising=False)
    assert _evaluate("S4")["position_multiplier"] == pytest.approx(1.0)


def test_fermentation_day_mock_allows_the_widest_band(monkeypatch):
    monkeypatch.setenv("HERMES_ENV_POSITION_TABLE", "enforce")
    result = _evaluate("S2")
    assert result["position_multiplier"] * 100.0 == pytest.approx(70.0)


def test_unknown_state_fails_closed_to_zero_when_enforced(monkeypatch):
    monkeypatch.setenv("HERMES_ENV_POSITION_TABLE", "enforce")
    result = _evaluate("S9")
    assert result["position_multiplier"] == pytest.approx(0.0)
    assert "env_position_table_unknown" in result["reasons"]


def test_table_never_loosens_an_existing_guardrail(monkeypatch):
    """全表只能收紧：既有门禁已把倍率打到 0 时，表不得把它抬回去。"""
    monkeypatch.setenv("HERMES_ENV_POSITION_TABLE", "enforce")
    result = _evaluate(
        "S2",
        portfolio_risk={"allowed": False, "status": "blocked",
                        "reasons": ["single_position_limit"]},
    )
    assert result["position_multiplier"] == pytest.approx(0.0)


def test_table_reason_is_classified_in_the_guardrail_grouping(monkeypatch):
    monkeypatch.setenv("HERMES_ENV_POSITION_TABLE", "enforce")
    result = _evaluate("S6", requested_action="buy", raw_score=88.0)
    codes = {item["code"] for item in (result["guardrail"] or {}).get("reasons", [])}
    # decision 未变时不带 guardrail；这里 S6 只降仓不改动作，故走 None 分支。
    assert result["guardrail"] is None or "temperature_gate" in codes
    assert decision_policy._guardrail_reason_code(
        "env_position_table_S6") == "temperature_gate"
