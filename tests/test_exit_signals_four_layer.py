"""P4(d) 四层止损 + 事件止损优先 — 升级方案 §7.1(d)。

四层：市场层(sentiment_exit/flow_reversal) → 题材层(theme_invalid) →
龙头层(leader_invalid/event_stop/lhb_climax) → 个股层(stop_loss/trailing/…)。
"""

import pytest

from exit_signals import (
    EXIT_LAYERS,
    check_event_stop,
    check_leader_invalid,
    check_theme_invalid,
    evaluate_all_exit_signals,
)


# ── 题材层 ──────────────────────────────────────────────────────────────────

def test_theme_score_collapse_triggers():
    result = check_theme_invalid(theme_score_drop_pct=25.0)
    assert result["triggered"] is True
    assert result["severity"] == "critical"
    assert result["action"] == "sell"


def test_theme_score_drop_below_threshold_does_not_trigger():
    assert check_theme_invalid(theme_score_drop_pct=19.9)["triggered"] is False


def test_theme_demotion_alone_is_not_enough():
    """排名波动是题材轮动的常态；只有降级 + 助攻大面积掉队才算主线塌方。"""
    assert check_theme_invalid(theme_rank_demoted=True)["triggered"] is False
    assert check_theme_invalid(theme_rank_demoted=True,
                               assist_laggard_ratio=0.3)["triggered"] is False
    assert check_theme_invalid(theme_rank_demoted=True,
                               assist_laggard_ratio=0.6)["triggered"] is True


def test_theme_laggard_ratio_alone_is_not_enough():
    assert check_theme_invalid(assist_laggard_ratio=0.9)["triggered"] is False


# ── 龙头层 ──────────────────────────────────────────────────────────────────

def test_leader_score_collapse_triggers():
    assert check_leader_invalid(leader_score_drop_pct=20.0)["triggered"] is True


def test_leader_streak_break_needs_support_break():
    assert check_leader_invalid(leader_streak_broken=True)["triggered"] is False
    assert check_leader_invalid(bid_support_broken=True)["triggered"] is False
    assert check_leader_invalid(leader_streak_broken=True,
                                bid_support_broken=True)["triggered"] is True


# ── 事件止损 ────────────────────────────────────────────────────────────────

def test_event_stop_requires_all_three_conditions():
    full = dict(leader_gap_pct=-6.0, assist_premium_pct=-1.0, laggard_limit_down=True)
    assert check_event_stop(**full)["triggered"] is True
    assert check_event_stop(**{**full, "leader_gap_pct": -1.0})["triggered"] is False
    assert check_event_stop(**{**full, "assist_premium_pct": 2.0})["triggered"] is False
    assert check_event_stop(**{**full, "laggard_limit_down": False})["triggered"] is False


def test_event_stop_missing_data_does_not_default_to_triggered():
    """这条规则强到可以越过价格止损，缺数据不能把它推成默认成立。"""
    assert check_event_stop(laggard_limit_down=True)["triggered"] is False
    assert check_event_stop(leader_gap_pct=-6.0,
                            laggard_limit_down=True)["triggered"] is False


# ── 事件止损优先于价格止损 ──────────────────────────────────────────────────

def test_event_stop_wins_when_atr_stop_untouched():
    """未触 ATR 止损、未触回撤止盈，但龙头失效 + 助攻无溢价 → 仍然退出。"""
    result = evaluate_all_exit_signals(
        current_price=20.0,
        stop_price=17.0,          # 远未触及
        target_price=30.0,
        peak_price=20.5,          # 回撤 ~2.4%，未触 5% 移动止损
        trailing_pct=5.0,
        leader_gap_pct=-6.0,
        assist_premium_pct=-1.5,
        laggard_limit_down=True,
    )
    assert result["action"] == "sell"
    assert result["top_signal"]["signal_type"] == "event_stop"
    assert result["event_stop_priority"] is True
    assert result["top_signal"]["price_stop_bypassed"] is True
    # 价格层确实一条都没触发——退出完全由事件层驱动。
    price_layer = [s for s in result["signals"]
                   if s["exit_layer"] == "stock" and s.get("triggered")]
    assert price_layer == []


def test_event_stop_outranks_a_simultaneous_price_stop():
    result = evaluate_all_exit_signals(
        current_price=10.0,
        stop_price=10.5,          # 价格止损同时触发（critical）
        leader_score_drop_pct=30.0,
    )
    assert result["top_signal"]["signal_type"] == "leader_invalid"
    assert {s["signal_type"] for s in result["signals"] if s.get("triggered")} >= {
        "stop_loss", "leader_invalid"}


def test_price_stop_still_wins_when_no_event_signal():
    """既有信号之间的相对次序一字未动：无事件信号时仍是价格止损排第一。"""
    result = evaluate_all_exit_signals(current_price=10.0, stop_price=10.5)
    assert result["top_signal"]["signal_type"] == "stop_loss"
    assert result["event_stop_priority"] is False


# ── 四层标签 ────────────────────────────────────────────────────────────────

def test_every_signal_carries_an_exit_layer():
    result = evaluate_all_exit_signals(current_price=10.0)
    assert all(s["exit_layer"] in {"market", "theme", "leader", "stock"}
               for s in result["signals"])
    assert EXIT_LAYERS["sentiment_exit"] == "market"
    assert EXIT_LAYERS["theme_invalid"] == "theme"
    assert EXIT_LAYERS["leader_invalid"] == "leader"
    assert EXIT_LAYERS["stop_loss"] == "stock"


def test_layers_triggered_reports_all_four_layers_in_order():
    result = evaluate_all_exit_signals(
        current_price=10.0,
        stop_price=10.5,                                  # 个股层
        temperature_tier="修复", prev_temperature_tier="加速",   # 市场层
        theme_score_drop_pct=30.0,                        # 题材层
        leader_score_drop_pct=30.0,                       # 龙头层
    )
    assert result["layers_triggered"] == ["market", "theme", "leader", "stock"]


def test_no_new_signal_fires_when_p4_inputs_are_absent():
    """向后兼容：不传 P4 参数时三条新信号一律不触发。"""
    result = evaluate_all_exit_signals(current_price=10.0, stop_price=1.0)
    new_signals = {"theme_invalid", "leader_invalid", "event_stop"}
    assert all(not s.get("triggered") for s in result["signals"]
               if s["signal_type"] in new_signals)
    assert result["action"] == "hold"


@pytest.mark.parametrize("value", [float("nan"), "20", True])
def test_non_numeric_drops_are_ignored_not_coerced(value):
    assert check_theme_invalid(theme_score_drop_pct=value)["triggered"] is False
    assert check_leader_invalid(leader_score_drop_pct=value)["triggered"] is False
