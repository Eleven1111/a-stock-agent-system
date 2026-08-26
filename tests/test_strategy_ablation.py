"""消融实验（P5-c）。

守三条会让消融结论作废的性质：阶梯必须真嵌套、样本不足不给结论、空集不出恒真数。
"""

from __future__ import annotations

import pytest

import strategy_ablation as ab


def _events(n=60, *, start=0):
    """构造 n 个事件，收益按下标交替正负，便于验证过滤真的改变了样本。"""
    return [
        {"code": f"{600000 + start + i:06d}", "date": "2026-06-01",
         "ret": 0.02 if i % 2 == 0 else -0.01,
         "sector_ok": i % 2 == 0, "sentiment_ok": i % 4 == 0}
        for i in range(n)
    ]


def _ret(row):
    return row["ret"]


# --------------------------------------------------------------------------- #
# 1) 阶梯必须真的嵌套
# --------------------------------------------------------------------------- #
def test_a_later_level_can_never_readmit_events_dropped_earlier():
    """即使某级谓词恒真，它也只能在上一级的存活集合上过滤，不能把已排除的放回来。

    这是消融各级可比的前提：样本必须单调收缩。写成运行时守卫是死代码（结构上不可能
    违反），所以这里直接断言这条不变量本身。
    """
    events = _events(10)
    predicates = {
        "A_limit_up_only": lambda row: row["sector_ok"],   # 先削掉一半
        "B_sector_breadth": lambda row: True,              # 恒真，试图"放宽"
    }
    result = ab.run_ablation(events, predicates, _ret,
                             ladder=("A_limit_up_only", "B_sector_breadth"),
                             min_samples=1)
    first = result["levels"]["A_limit_up_only"]["n"]
    second = result["levels"]["B_sector_breadth"]["n"]
    assert first == 5, "前置：A 级必须真的削掉了一半，否则本用例恒真"
    assert second == first, "恒真谓词只能保持样本，绝不能把 A 级排除的事件放回来"


def test_每级样本必须是上一级的子集():
    events = _events(40)
    predicates = {
        "A_limit_up_only": lambda row: True,
        "B_sector_breadth": lambda row: row["sector_ok"],
        "C_sentiment_filter": lambda row: row["sentiment_ok"],
    }
    result = ab.run_ablation(events, predicates, _ret,
                             ladder=("A_limit_up_only", "B_sector_breadth",
                                     "C_sentiment_filter"),
                             min_samples=1)
    counts = [result["levels"][name]["n"] for name in
              ("A_limit_up_only", "B_sector_breadth", "C_sentiment_filter")]
    assert counts == sorted(counts, reverse=True), counts
    assert counts[0] == 40 and counts[1] == 20 and counts[2] == 10


def test_missing_predicate_is_reported_not_silently_skipped():
    """缺某一级的谓词要显式标出来——静默跳过会让阶梯看起来完整、实际少一环。"""
    result = ab.run_ablation(_events(10), {"A_limit_up_only": lambda row: True},
                             _ret, ladder=("A_limit_up_only", "B_sector_breadth"),
                             min_samples=1)
    assert result["missing_predicates"] == ["B_sector_breadth"]
    assert result["levels"]["B_sector_breadth"]["status"] == ab.UNAVAILABLE
    assert result["levels"]["B_sector_breadth"]["reason"] == "predicate_not_supplied"


# --------------------------------------------------------------------------- #
# 2) 样本不足不给结论
# --------------------------------------------------------------------------- #
def test_level_below_min_samples_is_unverified_and_withholds_numbers():
    """逐级过滤会把样本削薄，到 G 级常剩个位数——此时 ΔPF 的正负号毫无意义。"""
    metrics = ab.level_metrics([0.01] * 5, min_samples=30)
    assert metrics["status"] == ab.UNVERIFIED
    assert metrics["mean"] is None
    assert metrics["profit_factor"]["value"] is None
    assert metrics["withheld_reason"] == "n<30"


def test_level_at_threshold_reports_numbers():
    metrics = ab.level_metrics([0.02] * 30, min_samples=30)
    assert metrics["status"] == ab.AVAILABLE
    assert metrics["mean"] == pytest.approx(0.02)


def test_delta_is_unavailable_when_either_side_is_withheld():
    """一侧被扣住时差值也不可用——拿 0 顶替会让"无改善"和"未知"混为一谈。"""
    events = _events(40)
    predicates = {
        "A_limit_up_only": lambda row: True,
        "B_sector_breadth": lambda row: row["sentiment_ok"],   # 削到 10 个
    }
    result = ab.run_ablation(events, predicates, _ret,
                             ladder=("A_limit_up_only", "B_sector_breadth"),
                             min_samples=30)
    delta = result["levels"]["B_sector_breadth"]["delta_vs_previous"]
    assert delta["profit_factor"]["status"] == ab.UNAVAILABLE
    assert delta["profit_factor"]["value"] is None


# --------------------------------------------------------------------------- #
# 3) 空集与全胜不出恒真数
# --------------------------------------------------------------------------- #
def test_empty_sample_metrics_are_unavailable_not_zero():
    for call in (ab.profit_factor, ab.max_drawdown, ab.tail_loss):
        result = call([])
        assert result["status"] == ab.UNAVAILABLE
        assert result["value"] is None


def test_profit_factor_without_losses_is_unavailable_not_infinite():
    """全胜时返回 inf 会污染下游一整列 delta。"""
    result = ab.profit_factor([0.01, 0.02])
    assert result["status"] == ab.UNAVAILABLE
    assert result["reason"] == "no_losing_trades"


def test_profit_factor_matches_hand_calculation():
    result = ab.profit_factor([0.04, 0.02, -0.03])
    assert result["value"] == pytest.approx(0.06 / 0.03)


def test_max_drawdown_is_path_dependent_not_worst_single_trade():
    """回撤要按顺序复利算：连续两笔 -10% 的伤害大于单笔 -15%。"""
    consecutive = ab.max_drawdown([-0.10, -0.10])["value"]
    single = ab.max_drawdown([-0.15])["value"]
    assert consecutive < single


def test_tail_loss_reports_the_left_tail_not_the_mean():
    returns = [-0.20] + [0.01] * 19
    assert ab.tail_loss(returns, quantile=0.05)["value"] == pytest.approx(-0.20)
    assert ab.tail_loss(returns)["n_tail"] == 1
