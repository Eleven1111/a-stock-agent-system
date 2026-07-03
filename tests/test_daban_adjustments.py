from datetime import date

import daban_adjustments as adj
import weak_market_delivery
from exit_signals import evaluate_all_exit_signals


GATE_ON = {
    "enabled": True,
    "blocked_theme_stages": ["diverging", "fading"],
    "min_temperature_score": 40.0,
}
EXIT_ON = {
    "enabled": True,
    "min_premium_pct": 3.0,
    "full_exit_premium_pct": 6.0,
}


def test_all_mechanisms_default_off():
    gate = adj.regime_gate_assessment(temperature_score=None, theme_stage="fading")
    assert gate == {"enabled": False, "blocked": False, "reasons": []}
    assert adj.entry_mode_multiplier("first_board_reseal") == 1.0
    signal = adj.check_auction_premium_exit(
        entry_date="2026-07-02", open_premium_pct=9.0, asof="2026-07-03",
    )
    assert signal["triggered"] is False


def test_regime_gate_blocks_low_temperature_and_fading_stage():
    cold = adj.regime_gate_assessment(
        temperature_score=25.0, theme_stage=None, config=GATE_ON,
    )
    assert cold["blocked"] is True
    assert any("市场温度" in reason for reason in cold["reasons"])

    fading = adj.regime_gate_assessment(
        temperature_score=80.0, theme_stage="fading", config=GATE_ON,
    )
    assert fading["blocked"] is True
    assert any("fading" in reason for reason in fading["reasons"])

    healthy = adj.regime_gate_assessment(
        temperature_score=80.0, theme_stage="mainline", config=GATE_ON,
    )
    assert healthy["blocked"] is False


def test_regime_gate_fails_closed_on_missing_temperature():
    gate = adj.regime_gate_assessment(
        temperature_score=None, theme_stage=None, config=GATE_ON,
    )
    assert gate["blocked"] is True
    assert any("不可用" in reason for reason in gate["reasons"])


def test_regime_gate_no_theme_is_not_blocked():
    gate = adj.regime_gate_assessment(
        temperature_score=80.0, theme_stage=None, config=GATE_ON,
    )
    assert gate["blocked"] is False


def test_entry_mode_multiplier_config_gated():
    cfg = {
        "enabled": True,
        "weights": {"first_board_reseal": 1.2, "second_board_weak_to_strong": 0.7},
    }
    assert adj.entry_mode_multiplier("first_board_reseal", cfg) == 1.2
    assert adj.entry_mode_multiplier("second_board_weak_to_strong", cfg) == 0.7
    assert adj.entry_mode_multiplier("unknown_pattern", cfg) == 1.0
    assert adj.entry_mode_multiplier(None, cfg) == 1.0
    assert adj.entry_mode_multiplier("first_board_reseal", {"enabled": False}) == 1.0


def test_auction_premium_exit_tiers_and_t1_rule():
    partial = adj.check_auction_premium_exit(
        entry_date="2026-07-02", open_premium_pct=3.5,
        asof="2026-07-03", config=EXIT_ON,
    )
    assert partial["triggered"] is True
    assert partial["severity"] == "warning"
    assert partial["action"] == "sell"

    full = adj.check_auction_premium_exit(
        entry_date="2026-07-02", open_premium_pct=7.2,
        asof="2026-07-03", config=EXIT_ON,
    )
    assert full["severity"] == "critical"

    below = adj.check_auction_premium_exit(
        entry_date="2026-07-02", open_premium_pct=1.0,
        asof="2026-07-03", config=EXIT_ON,
    )
    assert below["triggered"] is False

    same_day = adj.check_auction_premium_exit(
        entry_date="2026-07-03", open_premium_pct=9.0,
        asof="2026-07-03", config=EXIT_ON,
    )
    assert same_day["triggered"] is False  # T+1 铁律：当日买入不得卖出


def test_evaluate_all_exit_signals_includes_auction_premium(monkeypatch):
    monkeypatch.setattr(
        adj, "_daban_section",
        lambda name: {"auction_premium_exit": EXIT_ON} if name == "adjustments" else {},
    )
    result = evaluate_all_exit_signals(
        current_price=11.0,
        entry_date="2026-07-02",
        asof=date(2026, 7, 3),
        auction_open_premium_pct=6.5,
    )
    assert result["action"] == "sell"
    types = {s["signal_type"] for s in result["signals"] if s.get("triggered")}
    assert "auction_premium_exit" in types


def test_delivery_gate_wiring_downgrades_daban_lane(monkeypatch):
    monkeypatch.setattr(
        adj, "_daban_section",
        lambda name: {"regime_gate": GATE_ON} if name == "adjustments" else {},
    )
    item = {
        "code": "600001",
        "name": "测试股",
        "qualified": True,
        "hot_money_qualified": True,
        "theme_stage": "fading",
        "market_timing": {"temperature": {"score": 80.0}},
    }
    result = weak_market_delivery.assess_delivery_quality(
        item, lane="daban", stage="d0",
    )
    assert result["status"] == "research_only"
    assert any("fading" in reason for reason in result["reasons"])

    trend = weak_market_delivery.assess_delivery_quality(
        item, lane="trend", stage="d0",
    )
    assert not any("regime_gate" in reason for reason in trend["reasons"])
