"""纪律分（P6）。

守四条：无数据不给满分、分数不可用时保守处理、纠错周只看最近连续段、
与 behavior_risk 取更保守者而非相乘（防同一个坏日子被罚两次）。
"""

from __future__ import annotations

import pytest

import discipline_score as ds


# --------------------------------------------------------------------------- #
# 1) "今天没违规" ≠ "今天没数据"
# --------------------------------------------------------------------------- #
def test_missing_execution_records_is_unavailable_not_full_marks():
    """系统失灵、什么都没记上的那天，绝不能拿满分和最宽松的仓位。"""
    result = ds.score_day({}, executed_trade_count=None)
    assert result["status"] == ds.UNAVAILABLE
    assert result["score"] is None
    assert result["reason"] == "execution_records_missing"


def test_clean_day_with_real_trades_scores_100():
    result = ds.score_day({}, executed_trade_count=3)
    assert result["status"] == ds.AVAILABLE
    assert result["score"] == 100


def test_zero_trade_day_with_records_is_still_scored():
    """当日没交易但有执行记录 → 是"零违规"，可以给分。"""
    assert ds.score_day({}, executed_trade_count=0)["score"] == 100


# --------------------------------------------------------------------------- #
# 2) 扣分口径
# --------------------------------------------------------------------------- #
def test_penalties_match_the_book_weights():
    result = ds.score_day(
        {"off_system_trade": 1, "impulsive_trade": 1,
         "unplanned_add": 1, "late_chase": 1},
        executed_trade_count=4)
    assert result["deductions"] == {"off_system_trade": 20, "impulsive_trade": 10,
                                    "unplanned_add": 10, "late_chase": 10}
    assert result["score"] == 50


def test_score_is_floored_at_zero_but_raw_score_keeps_severity():
    """截断到 0 便于展示，但原始值保留——连续违规的严重程度不该被 0 抹平。"""
    result = ds.score_day({"off_system_trade": 8}, executed_trade_count=8)
    assert result["score"] == 0
    assert result["raw_score"] == 100 - 160


# --------------------------------------------------------------------------- #
# 3) 次日动作与保守默认
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("score,action,mult", [
    (100, "normal", 1.0),
    (80, "normal", 1.0),          # 边界：等于 80 不减半
    (79, "halve_position", 0.5),
    (60, "halve_position", 0.5),  # 边界：等于 60 仍只减半
    (59, "paper_only", 0.0),
])
def test_next_day_action_thresholds(score, action, mult):
    result = ds.next_day_action({"status": ds.AVAILABLE, "score": score})
    assert result["action"] == action
    assert result["position_multiplier"] == mult


def test_unavailable_score_defaults_to_halving_not_business_as_usual():
    """分数算不出来时保守减半——照常放行等于把"未知"当成"没问题"。"""
    result = ds.next_day_action({"status": ds.UNAVAILABLE, "score": None})
    assert result["action"] == "halve_position"
    assert result["position_multiplier"] == 0.5
    assert result["status"] == ds.UNAVAILABLE


# --------------------------------------------------------------------------- #
# 4) 纠错周只看最近连续段
# --------------------------------------------------------------------------- #
def test_correction_week_triggers_on_three_consecutive_low_days():
    result = ds.needs_correction_week([90, 70, 65, 75])
    assert result["streak"] == 3
    assert result["triggered"] is True


def test_a_good_day_breaks_the_streak():
    """中间回到 80 以上，连续性就断了——更早的低分日不该无限期累积成惩罚。"""
    result = ds.needs_correction_week([70, 65, 85, 70])
    assert result["streak"] == 1
    assert result["triggered"] is False


def test_empty_history_is_unavailable_not_false():
    result = ds.needs_correction_week([])
    assert result["status"] == ds.UNAVAILABLE
    assert result["triggered"] is None


# --------------------------------------------------------------------------- #
# 5) 与 behavior_risk 合并：取更保守，不相乘
# --------------------------------------------------------------------------- #
def test_combined_takes_the_more_conservative_multiplier():
    action = ds.next_day_action({"status": ds.AVAILABLE, "score": 79})   # 0.5
    combined = ds.combined_position_multiplier(action, behavior_multiplier=0.2)
    assert combined["position_multiplier"] == pytest.approx(0.2)
    assert combined["source"] == "behavior_risk"


def test_combined_never_multiplies_the_two_penalties():
    """相乘会把同一个坏日子罚两次：0.5 × 0.5 = 0.25，而正确答案是 0.5。"""
    action = ds.next_day_action({"status": ds.AVAILABLE, "score": 79})
    combined = ds.combined_position_multiplier(action, behavior_multiplier=0.5)
    assert combined["position_multiplier"] == pytest.approx(0.5)
    assert combined["position_multiplier"] != pytest.approx(0.25)


def test_combined_works_when_behavior_risk_is_absent():
    action = ds.next_day_action({"status": ds.AVAILABLE, "score": 100})
    combined = ds.combined_position_multiplier(action, behavior_multiplier=None)
    assert combined["position_multiplier"] == pytest.approx(1.0)
    assert combined["source"] == "discipline"


def test_combined_unavailable_when_neither_side_has_a_multiplier():
    combined = ds.combined_position_multiplier({}, behavior_multiplier=None)
    assert combined["status"] == ds.UNAVAILABLE
    assert combined["position_multiplier"] is None
