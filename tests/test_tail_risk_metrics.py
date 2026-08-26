"""尾部风险指标（P5-d）。

守的性质集中在一点：**空样本不得显示成零风险**。把"没有样本"渲染成
`limit_down_risk = 0.0`，方向恰好是危险的那一侧——它会让一个从未被检验过的策略
看起来没有尾部风险。
"""

from __future__ import annotations

import pytest

import tail_risk_metrics as trm


# --------------------------------------------------------------------------- #
# 1) 空集纪律：unavailable 而不是 0.0
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("call", [
    lambda: trm.gap_risk([]),
    lambda: trm.limit_down_risk([]),
    lambda: trm.mae([]),
    lambda: trm.mfe([]),
    lambda: trm.avg_r([], []),
    lambda: trm.expectancy([]),
])
def test_empty_sample_is_unavailable_never_zero(call):
    result = call()
    assert result["status"] == trm.UNAVAILABLE
    assert result["value"] is None, "空样本给出数值＝把未知尾部风险显示成零风险"
    assert result["n"] == 0


def test_all_none_input_is_treated_as_empty():
    assert trm.gap_risk([None, None])["status"] == trm.UNAVAILABLE
    assert trm.limit_down_risk([None, None])["status"] == trm.UNAVAILABLE


# --------------------------------------------------------------------------- #
# 2) GapRisk / LimitDownRisk：条件概率算对
# --------------------------------------------------------------------------- #
def test_gap_risk_counts_only_gaps_at_or_below_threshold():
    gaps = [-8.0, -5.0, -4.9, 0.0, 3.0]
    result = trm.gap_risk(gaps, threshold_pct=-5.0)
    assert result["status"] == trm.AVAILABLE
    assert result["hits"] == 2          # -8.0 与 -5.0（含等号）
    assert result["n"] == 5
    assert result["value"] == pytest.approx(0.4)


def test_gap_risk_threshold_is_configurable_and_reported():
    result = trm.gap_risk([-3.0, -1.0], threshold_pct=-2.0)
    assert result["threshold_pct"] == -2.0
    assert result["value"] == pytest.approx(0.5)


def test_limit_down_risk_ignores_missing_flags_in_denominator():
    """缺失标记不能计入分母，否则概率被系统性稀释（看起来更安全）。"""
    result = trm.limit_down_risk([True, False, None, False])
    assert result["n"] == 3
    assert result["value"] == pytest.approx(1 / 3)


# --------------------------------------------------------------------------- #
# 3) MAE/MFE 必须取路径极值，不是收盘值
# --------------------------------------------------------------------------- #
def test_mae_uses_path_extreme_not_final_value():
    """一笔最终收平、盘中一度 -12% 的交易，收盘口径完全看不见它。"""
    flat_close_but_deep_dip = [0.0, -12.0, -3.0, 0.0]
    result = trm.mae([flat_close_but_deep_dip])
    assert result["value"] == pytest.approx(-12.0)
    assert result["worst"] == pytest.approx(-12.0)


def test_mfe_uses_path_maximum():
    assert trm.mfe([[0.0, 9.0, 2.0]])["value"] == pytest.approx(9.0)


def test_mae_averages_across_trades():
    assert trm.mae([[0.0, -4.0], [0.0, -8.0]])["value"] == pytest.approx(-6.0)


# --------------------------------------------------------------------------- #
# 4) Avg R：除零污染必须被挡住
# --------------------------------------------------------------------------- #
def test_avg_r_drops_non_positive_risk_records_and_counts_them():
    result = trm.avg_r([3.0, 3.0], [1.0, 0.0])
    assert result["n"] == 1
    assert result["dropped_non_positive_risk"] == 1
    assert result["value"] == pytest.approx(3.0)


def test_avg_r_makes_different_volatility_comparable():
    """同样赚 3%，1% 风险下是 3R，6% 风险下只有 0.5R。"""
    tight = trm.avg_r([3.0], [1.0])["value"]
    loose = trm.avg_r([3.0], [6.0])["value"]
    assert tight > loose


def test_avg_r_length_mismatch_is_unavailable():
    assert trm.avg_r([1.0, 2.0], [1.0])["status"] == trm.UNAVAILABLE


def test_avg_r_all_risk_zero_is_unavailable_not_infinite():
    result = trm.avg_r([5.0], [0.0])
    assert result["status"] == trm.UNAVAILABLE
    assert result["value"] is None


# --------------------------------------------------------------------------- #
# 5) 期望值
# --------------------------------------------------------------------------- #
def test_expectancy_matches_hand_calculation():
    # 2 胜(+4,+6, 均值5) / 2 负(-2,-4, 均值3)，胜率 0.5 → 0.5*5 - 0.5*3 = 1.0
    result = trm.expectancy([4.0, 6.0, -2.0, -4.0])
    assert result["value"] == pytest.approx(1.0)
    assert result["win_rate"] == pytest.approx(0.5)


def test_expectancy_counts_flat_trades_in_denominator():
    """平局压低胜率但不进盈亏两侧——把它剔除会高估期望值。"""
    with_flat = trm.expectancy([4.0, 0.0, -2.0])
    assert with_flat["win_rate"] == pytest.approx(1 / 3)


# --------------------------------------------------------------------------- #
# 6) State PnL：低于门槛扣住均值
# --------------------------------------------------------------------------- #
def test_state_pnl_withholds_mean_below_threshold():
    records = [{"state": "发酵", "return_pct": 1.0} for _ in range(5)]
    cells = trm.state_pnl(records, min_samples=30)["cells"]
    assert cells["发酵"]["status"] == "UNVERIFIED"
    assert cells["发酵"]["mean"] is None
    assert cells["发酵"]["n"] == 5


def test_state_pnl_reports_mean_once_threshold_reached():
    records = [{"state": "发酵", "return_pct": 2.0} for _ in range(30)]
    cells = trm.state_pnl(records, min_samples=30)["cells"]
    assert cells["发酵"]["status"] == trm.AVAILABLE
    assert cells["发酵"]["mean"] == pytest.approx(2.0)


def test_state_pnl_separates_states_and_skips_incomplete_rows():
    records = [
        {"state": "发酵", "return_pct": 1.0},
        {"state": "退潮", "return_pct": -1.0},
        {"state": None, "return_pct": 5.0},        # 无状态，丢弃
        {"state": "发酵", "return_pct": None},      # 无收益，丢弃
    ]
    cells = trm.state_pnl(records, min_samples=1)["cells"]
    assert set(cells) == {"发酵", "退潮"}
    assert cells["发酵"]["n"] == 1


def test_state_pnl_empty_is_unavailable():
    assert trm.state_pnl([])["status"] == trm.UNAVAILABLE
