"""P4 仓位管理与风控阶梯（position_risk）单测 — 升级方案 §7.2。

覆盖四块：1+1+1 状态机的全部非法转移、R 化仓位的 fail-closed 与手算算例、
环境总仓表、熔断阶梯与倍率合并。四层止损的断言在
tests/test_exit_signals_four_layer.py。
"""

import pytest

import position_risk as pr


# ── (a) 1+1+1 加仓状态机 ─────────────────────────────────────────────────────

def _ctx(**overrides):
    base = {
        "signal_valid": True,
        "sector_confirmed": True,
        "leader_confirmed": True,
        "market_deteriorated": False,
        "logic_still_valid": True,
        "unrealized_pnl_pct": 3.0,
    }
    base.update(overrides)
    return base


def _fill_all_three():
    ladder = pr.new_ladder("600519")
    for leg in pr.LEG_ORDER:
        result = pr.apply_leg(ladder, leg, _ctx())
        assert result["decision"]["allowed"], (leg, result["decision"]["reasons"])
        ladder = result["ladder"]
    return ladder


def test_happy_path_three_legs_reach_30_pct():
    ladder = _fill_all_three()
    assert ladder["filled_legs"] == list(pr.LEG_ORDER)
    assert ladder["position_pct"] == pytest.approx(30.0)
    assert ladder["locked"] is False


def test_ladder_starts_empty_and_immutable():
    ladder = pr.new_ladder("000001")
    result = pr.apply_leg(ladder, "logic_leg", _ctx())
    assert result["ladder"]["position_pct"] == pytest.approx(10.0)
    # 入参不被修改：状态机是不可变风格，调用方持有的旧状态必须原样保留。
    assert ladder["position_pct"] == pytest.approx(0.0)
    assert ladder["filled_legs"] == []


# —— 非法转移 1：亏损加仓 ——

@pytest.mark.parametrize("leg", ["confirm_leg", "profit_leg"])
def test_losing_add_is_forbidden(leg):
    ladder = pr.new_ladder("600519")
    ladder = pr.apply_leg(ladder, "logic_leg", _ctx())["ladder"]
    if leg == "profit_leg":
        ladder = pr.apply_leg(ladder, "confirm_leg", _ctx())["ladder"]
    result = pr.apply_leg(ladder, leg, _ctx(unrealized_pnl_pct=-2.0))
    assert result["decision"]["allowed"] is False
    assert "losing_add_forbidden" in result["decision"]["reasons"]


def test_losing_add_locks_ladder_permanently():
    """浮亏关闭的是**永久**开关，不是本次跳过：随后转盈也不再放行。"""
    ladder = pr.new_ladder("600519")
    ladder = pr.apply_leg(ladder, "logic_leg", _ctx())["ladder"]
    blocked = pr.apply_leg(ladder, "confirm_leg", _ctx(unrealized_pnl_pct=-2.0))
    assert blocked["ladder"]["locked"] is True
    assert blocked["ladder"]["lock_reason"] == "losing_add_forbidden"

    recovered = pr.apply_leg(blocked["ladder"], "confirm_leg",
                             _ctx(unrealized_pnl_pct=+8.0))
    assert recovered["decision"]["allowed"] is False
    assert recovered["decision"]["reasons"] == ["losing_add_forbidden"]
    assert recovered["ladder"]["position_pct"] == pytest.approx(10.0)


def test_missing_pnl_fails_closed_but_does_not_lock():
    """缺浮盈数据 ≠ 浮亏：拒绝本次，但不永久上锁。"""
    ladder = pr.apply_leg(pr.new_ladder("600519"), "logic_leg", _ctx())["ladder"]
    result = pr.apply_leg(ladder, "confirm_leg", _ctx(unrealized_pnl_pct=None))
    assert result["decision"]["allowed"] is False
    assert "unrealized_pnl_unavailable" in result["decision"]["reasons"]
    assert result["ladder"]["locked"] is False


# —— 非法转移 2：跳级加仓 ——

def test_skip_to_profit_leg_is_rejected():
    ladder = pr.apply_leg(pr.new_ladder("600519"), "logic_leg", _ctx())["ladder"]
    result = pr.apply_leg(ladder, "profit_leg", _ctx())
    assert result["decision"]["allowed"] is False
    assert "leg_out_of_order" in result["decision"]["reasons"]
    assert result["ladder"]["position_pct"] == pytest.approx(10.0)


def test_confirm_leg_before_logic_leg_is_rejected():
    result = pr.apply_leg(pr.new_ladder("600519"), "confirm_leg", _ctx())
    assert result["decision"]["allowed"] is False
    assert "leg_out_of_order" in result["decision"]["reasons"]


def test_duplicate_leg_is_rejected():
    ladder = pr.apply_leg(pr.new_ladder("600519"), "logic_leg", _ctx())["ladder"]
    result = pr.apply_leg(ladder, "logic_leg", _ctx())
    assert result["decision"]["allowed"] is False
    assert "leg_already_filled" in result["decision"]["reasons"]


def test_unknown_leg_is_rejected():
    result = pr.apply_leg(pr.new_ladder("600519"), "moon_leg", _ctx())
    assert result["decision"]["reasons"] == ["unknown_leg"]


# —— 非法转移 3：超单股上限 ——

def test_single_position_cap_blocks_fourth_leg_worth_of_size():
    ladder = pr.new_ladder("600519", leg_pct=10.0, max_single_position_pct=20.0)
    ladder = pr.apply_leg(ladder, "logic_leg", _ctx())["ladder"]
    ladder = pr.apply_leg(ladder, "confirm_leg", _ctx())["ladder"]
    assert ladder["position_pct"] == pytest.approx(20.0)
    result = pr.apply_leg(ladder, "profit_leg", _ctx())
    assert result["decision"]["allowed"] is False
    assert "single_position_cap_exceeded" in result["decision"]["reasons"]


def test_default_cap_is_30_pct():
    ladder = _fill_all_three()
    # 三条腿刚好打满 30%，第四条腿（哪怕腿名合法）无处可去。
    result = pr.apply_leg({**ladder, "filled_legs": []}, "logic_leg", _ctx())
    assert result["decision"]["allowed"] is False
    assert "single_position_cap_exceeded" in result["decision"]["reasons"]


# —— 每条腿自己的成立条件 ——

def test_confirm_leg_requires_all_three_confirmations():
    ladder = pr.apply_leg(pr.new_ladder("600519"), "logic_leg", _ctx())["ladder"]
    result = pr.apply_leg(ladder, "confirm_leg", _ctx(
        sector_confirmed=False, leader_confirmed=False, market_deteriorated=True))
    assert result["decision"]["allowed"] is False
    assert set(result["decision"]["reasons"]) == {
        "sector_not_confirmed", "leader_not_confirmed", "market_deteriorated"}


def test_profit_leg_requires_profit_and_live_logic():
    ladder = pr.apply_leg(pr.new_ladder("600519"), "logic_leg", _ctx())["ladder"]
    ladder = pr.apply_leg(ladder, "confirm_leg", _ctx())["ladder"]
    flat = pr.apply_leg(ladder, "profit_leg", _ctx(unrealized_pnl_pct=0.0))
    assert "no_unrealized_profit" in flat["decision"]["reasons"]
    stale = pr.apply_leg(ladder, "profit_leg", _ctx(logic_still_valid=False))
    assert "logic_no_longer_valid" in stale["decision"]["reasons"]


def test_logic_leg_requires_valid_signal():
    result = pr.apply_leg(pr.new_ladder("600519"), "logic_leg",
                          _ctx(signal_valid=False))
    assert result["decision"]["reasons"] == ["signal_not_valid"]


# —— 审计事件 ——

def test_ladder_events_cover_filled_and_blocked_legs():
    ladder = pr.apply_leg(pr.new_ladder("600519"), "logic_leg", _ctx())
    filled = pr.ladder_leg_event(ladder["decision"], {"signal_id": "sig-1"})
    blocked_decision = pr.apply_leg(ladder["ladder"], "profit_leg", _ctx())["decision"]
    blocked = pr.ladder_leg_event(blocked_decision, {"signal_id": "sig-1"})

    assert filled["event_type"] == pr.LADDER_EVENT_TYPE
    assert filled["idempotency_key"].endswith(":logic_leg:filled")
    assert filled["payload"]["position_pct_after"] == pytest.approx(10.0)
    # 被拒的腿同样落账，否则「今天为什么没加仓」在回放里查不到。
    assert blocked["payload"]["allowed"] is False
    assert blocked["idempotency_key"].endswith(":profit_leg:blocked")
    assert filled["idempotency_key"] != blocked["idempotency_key"]


# ── (b) R 化风险预算 ─────────────────────────────────────────────────────────

def test_r_sizing_worked_example_risk_budget_binding():
    """手算：NAV=100000，RiskBudget=1.0% → 1000 元；StopDistance=5% → 仓位 20000 元。

    20000/100000 = 20%，小于 ModeCap 30% → 风险预算是约束方。
    """
    result = pr.r_sized_position(net_asset_value=100_000, stop_distance_pct=5.0,
                                 mode_cap_pct=30.0, risk_budget_pct=1.0)
    assert result["status"] == pr.AVAILABLE
    assert result["risk_budget_value"] == pytest.approx(1000.0)
    assert result["position_pct"] == pytest.approx(20.0)
    assert result["position_value"] == pytest.approx(20_000.0)
    assert result["binding"] == "risk_budget"


def test_r_sizing_worked_example_mode_cap_binding():
    """手算：rb=1.0%、sd=3% → 33.33%，被 ModeCap 30% 压回 30%。"""
    result = pr.r_sized_position(net_asset_value=200_000, stop_distance_pct=3.0,
                                 mode_cap_pct=30.0, risk_budget_pct=1.0)
    assert result["risk_sized_pct"] == pytest.approx(33.3333, abs=1e-3)
    assert result["position_pct"] == pytest.approx(30.0)
    assert result["position_value"] == pytest.approx(60_000.0)
    assert result["binding"] == "mode_cap"


@pytest.mark.parametrize("stop", [0.0, -5.0, None, "5"])
def test_stop_distance_non_positive_fails_closed(stop):
    """除零会得到无穷仓位——这里必须是 0 仓位 + blocked，不是「不设上限」。"""
    result = pr.r_sized_position(net_asset_value=100_000, stop_distance_pct=stop,
                                 mode_cap_pct=30.0, risk_budget_pct=1.0)
    assert result["status"] == pr.BLOCKED
    assert result["position_pct"] == 0.0
    assert result["position_value"] == 0.0
    assert "stop_distance_unavailable" in result["reasons"]
    assert result["binding"] == "fail_closed"


@pytest.mark.parametrize("nav", [0, -1, None])
def test_nav_unavailable_fails_closed(nav):
    result = pr.r_sized_position(net_asset_value=nav, stop_distance_pct=5.0,
                                 mode_cap_pct=30.0)
    assert result["status"] == pr.BLOCKED
    assert "net_asset_value_unavailable" in result["reasons"]


def test_mode_cap_zero_fails_closed():
    result = pr.r_sized_position(net_asset_value=100_000, stop_distance_pct=5.0,
                                 mode_cap_pct=0.0)
    assert result["status"] == pr.BLOCKED
    assert "mode_cap_unavailable" in result["reasons"]


def test_risk_budget_pct_is_clamped_to_research_range():
    high = pr.r_sized_position(net_asset_value=100_000, stop_distance_pct=5.0,
                               mode_cap_pct=100.0, risk_budget_pct=5.0)
    assert high["risk_budget_pct"] == pytest.approx(1.0)
    assert "risk_budget_pct_clamped" in high["reasons"]
    low = pr.r_sized_position(net_asset_value=100_000, stop_distance_pct=5.0,
                              mode_cap_pct=100.0, risk_budget_pct=0.1)
    assert low["risk_budget_pct"] == pytest.approx(0.5)


def test_stop_distance_prefers_structural_over_atr():
    result = pr.resolve_stop_distance_pct(structural_stop_pct=6.0, atr_pct=4.0)
    assert result["source"] == "structural"
    assert result["stop_distance_pct"] == pytest.approx(6.0)


def test_stop_distance_from_atr_uses_clamped_multiple():
    result = pr.resolve_stop_distance_pct(atr_pct=3.0, atr_multiple=9.0)
    assert result["atr_multiple"] == pytest.approx(2.0)   # 夹进 1.2-2.0
    assert result["raw_stop_distance_pct"] == pytest.approx(6.0)
    assert "atr_multiple_clamped" in result["notes"]


@pytest.mark.parametrize("atr,expected", [(1.0, 3.0), (10.0, 8.0)])
def test_stop_distance_clamped_into_3_to_8(atr, expected):
    result = pr.resolve_stop_distance_pct(atr_pct=atr, atr_multiple=1.5)
    assert result["stop_distance_pct"] == pytest.approx(expected)
    assert "stop_distance_clamped" in result["notes"]


def test_stop_distance_unavailable_returns_none_not_zero():
    result = pr.resolve_stop_distance_pct()
    assert result["status"] == pr.UNAVAILABLE
    assert result["stop_distance_pct"] is None


# ── (c) 环境总仓表 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("state,low,high", [
    ("S0", 0.0, 10.0), ("S1", 20.0, 40.0), ("S2", 40.0, 70.0),
    ("S3", 30.0, 60.0), ("S4", 0.0, 30.0), ("S5", 40.0, 70.0), ("S6", 0.0, 10.0),
])
def test_environment_table_covers_all_seven_states(state, low, high):
    band = pr.environment_position_band(state)
    assert band["status"] == pr.AVAILABLE
    assert (band["min_pct"], band["max_pct"]) == (low, high)


@pytest.mark.parametrize("tier,state", [
    ("冰点", "S0"), ("修复", "S1"), ("发酵", "S2"), ("加速", "S3"), ("极热", "S4")])
def test_tier_folds_into_the_same_table(tier, state):
    band = pr.environment_position_band(tier=tier)
    assert band["state"] == state
    assert band["resolved_from"] == "tier"


def test_unknown_state_fails_closed_to_zero():
    band = pr.environment_position_band("S9")
    assert band["status"] == pr.UNAVAILABLE
    assert band["max_pct"] == 0.0
    assert pr.environment_position_multiplier("S9")["position_multiplier"] == 0.0


def test_environment_multiplier_matches_band_ceiling():
    assert pr.environment_position_multiplier("S6")["position_multiplier"] == pytest.approx(0.10)
    assert pr.environment_position_multiplier("S2")["position_multiplier"] == pytest.approx(0.70)


# ── (e) 熔断阶梯 ────────────────────────────────────────────────────────────

def test_quiet_day_trips_nothing_but_still_records_every_rung():
    result = pr.assess_circuit_ladder(day_pnl_r=0.4, week_pnl_r=1.1,
                                      drawdown_pct=1.0, off_system_streak=0)
    assert result["triggered"] == []
    assert result["position_multiplier"] == pytest.approx(1.0)
    assert result["new_open_allowed"] is True
    # 未触发的档同样返回：回放要能对账「当天每一档各是什么状态」。
    assert {rung["rung"] for rung in result["rungs"]} >= {
        "day_loss_2r", "week_loss_reduce", "week_loss_freeze",
        "drawdown_halve", "drawdown_stop", "off_system_streak"}


def test_day_minus_2r_stops_new_opens():
    result = pr.assess_circuit_ladder(day_pnl_r=-2.0)
    assert "day_loss_2r" in result["triggered"]
    assert result["new_open_allowed"] is False
    assert result["position_multiplier"] == pytest.approx(0.0)


def test_week_minus_4r_reduces_but_minus_5r_freezes():
    reduce = pr.assess_circuit_ladder(week_pnl_r=-4.2)
    assert reduce["triggered"] == ["week_loss_reduce"]
    assert reduce["position_multiplier"] == pytest.approx(0.5)
    assert reduce["new_open_allowed"] is True
    freeze = pr.assess_circuit_ladder(week_pnl_r=-5.0)
    assert set(freeze["triggered"]) == {"week_loss_reduce", "week_loss_freeze"}
    assert freeze["new_open_allowed"] is False


def test_drawdown_8pct_halves_and_10pct_forces_review_week():
    halve = pr.assess_circuit_ladder(drawdown_pct=8.0)
    assert halve["position_multiplier"] == pytest.approx(0.5)
    assert halve["review_week_required"] is False
    stop = pr.assess_circuit_ladder(drawdown_pct=10.0)
    assert stop["review_week_required"] is True
    assert stop["live_trading_halted"] is True
    assert stop["position_multiplier"] == pytest.approx(0.0)


def test_theme_risk_cap_blocks_only_the_offending_theme():
    result = pr.assess_circuit_ladder(theme_risk_r={"AI算力": 2.4, "锂电": 1.1})
    assert result["blocked_themes"] == ["AI算力"]
    assert "theme_risk_cap:AI算力" in result["triggered"]
    assert "theme_risk_cap:锂电" not in result["triggered"]


def test_off_system_streak_of_three_halts_trading():
    assert pr.assess_circuit_ladder(off_system_streak=2)["new_open_allowed"] is True
    halted = pr.assess_circuit_ladder(off_system_streak=3)
    assert halted["new_open_allowed"] is False
    assert halted["live_trading_halted"] is True


def test_missing_inputs_are_not_treated_as_zero():
    """「今天没亏」和「今天没数据」必须可区分——缺数据的档 observed=None。"""
    result = pr.assess_circuit_ladder()
    by_name = {rung["rung"]: rung for rung in result["rungs"]}
    assert by_name["day_loss_2r"]["observed"] is None
    assert by_name["day_loss_2r"]["triggered"] is False
    assert result["position_multiplier"] == pytest.approx(1.0)


def test_circuit_events_are_one_per_rung_and_idempotent():
    result = pr.assess_circuit_ladder(day_pnl_r=-2.5, drawdown_pct=3.0)
    events = pr.circuit_ladder_events(result, {"asof": "2026-08-26"})
    assert len(events) == len(result["rungs"])
    keys = [event["idempotency_key"] for event in events]
    assert len(set(keys)) == len(keys)
    assert all(key.startswith("risk.circuit_rung:2026-08-26:") for key in keys)
    triggered = [e for e in events if e["payload"]["triggered"]]
    assert [e["payload"]["rung"] for e in triggered] == ["day_loss_2r"]


def test_thresholds_come_from_config_section():
    import daban_config

    cfg = daban_config.section("circuit_ladder_r")
    assert cfg["day_loss_r_stop"] == -2.0
    assert cfg["theme_risk_r_max"] == 2.0
    # 既有 market_gate 的百分比口径仍在，两套口径并存。
    assert daban_config.section("market_gate")["day_loss_pct_stop"] == -2.0


# ── 倍率合并：取更保守，不相乘 ────────────────────────────────────────────

def test_merge_takes_the_most_conservative_not_the_product():
    merged = pr.merge_position_multipliers(
        {"schema": "circuit", "position_multiplier": 0.5},
        {"source": "discipline", "position_multiplier": 0.5},
    )
    # 相乘会得到 0.25 —— 同一个坏日子被两套独立口径各罚一次。
    assert merged["position_multiplier"] == pytest.approx(0.5)


def test_merge_accepts_bare_numbers_and_discipline_output():
    import discipline_score

    action = discipline_score.next_day_action(
        discipline_score.score_day({"off_system_trade": 1, "late_chase": 1},
                                   executed_trade_count=3))
    assert action["position_multiplier"] == pytest.approx(0.5)
    circuit = pr.assess_circuit_ladder(drawdown_pct=10.0)
    merged = pr.merge_position_multipliers(circuit, action, 0.8)
    assert merged["position_multiplier"] == pytest.approx(0.0)


def test_merge_without_any_input_is_unavailable_not_full_size():
    merged = pr.merge_position_multipliers(None, {"position_multiplier": None})
    assert merged["status"] == pr.UNAVAILABLE
    assert merged["position_multiplier"] is None


# ── 主题在险汇总 ────────────────────────────────────────────────────────────

def test_theme_risk_from_positions_skips_positions_without_stop_distance():
    positions = [
        {"theme": "AI算力", "market_value": 30_000, "stop_distance_pct": 5.0},
        {"theme": "AI算力", "market_value": 20_000, "stop_distance_pct": 5.0},
        {"theme": "锂电", "market_value": 40_000},          # 无止损距离 → 跳过
    ]
    totals = pr.theme_risk_from_positions(positions, risk_unit_value=1000.0)
    # (30000+20000)*5% = 2500 元 = 2.5R
    assert totals == {"AI算力": pytest.approx(2.5)}
    assert pr.assess_circuit_ladder(theme_risk_r=totals)["blocked_themes"] == ["AI算力"]


def test_theme_risk_requires_positive_risk_unit():
    assert pr.theme_risk_from_positions([{"market_value": 1}], risk_unit_value=0) == {}


def test_tier_to_state_stays_in_sync_with_market_temperature():
    """两份 TIER_TO_STATE 必须逐键相等。

    position_risk 刻意保留一份局部映射以维持零依赖（cron / 回测路径可单独 import），
    代价是它可能与 market_temperature 的那份悄悄分叉。运行时不加 import，改由本用例
    守住同步——两份同值常量没有守卫时，迟早只改一边（EVENT_SCHEMA 就这么漂过）。
    """
    import market_temperature

    assert pr.TIER_TO_STATE == market_temperature.TIER_TO_STATE
