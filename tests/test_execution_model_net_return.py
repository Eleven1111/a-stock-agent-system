"""交易成本换算 — net_return_pct（纯函数，不触网）"""

import pytest

from execution_model import FEE_SCHEDULE, net_return_pct


ASOF = "2026-08-12"


def test_net_return_matches_hand_computed_round_trip():
    """与手算逐项对齐：买入佣金+过户费，卖出佣金+印花税+过户费。"""
    notional = 20_000.0
    gross_pct = 3.0
    entry_value = notional
    exit_value = notional * (1 + gross_pct / 100)
    buy = max(5.0, entry_value * 3.0 / 10_000) + entry_value * 0.1 / 10_000
    sell = (
        max(5.0, exit_value * 3.0 / 10_000)
        + exit_value * 5.0 / 10_000
        + exit_value * 0.1 / 10_000
    )
    expected = (exit_value - entry_value - buy - sell) / entry_value * 100

    result = net_return_pct(
        gross_return_pct=gross_pct, notional=notional, asof=ASOF
    )

    assert result["net_return_pct"] == pytest.approx(expected, abs=1e-4)
    assert result["gross_return_pct"] == gross_pct
    assert result["net_return_pct"] < gross_pct


def test_cost_percentage_is_reported_and_notional_dependent():
    """最低佣金 5 元使成本率随本金变化，故名义本金必须回显。"""
    small = net_return_pct(gross_return_pct=0.0, notional=5_000.0, asof=ASOF)
    large = net_return_pct(gross_return_pct=0.0, notional=200_000.0, asof=ASOF)

    assert small["assumed_notional"] == 5_000.0
    assert large["assumed_notional"] == 200_000.0
    assert small["cost_pct"] > large["cost_pct"]
    assert large["cost_pct"] > 0


def test_zero_gross_return_is_negative_after_cost():
    result = net_return_pct(gross_return_pct=0.0, notional=20_000.0, asof=ASOF)

    assert result["net_return_pct"] < 0
    assert result["cost_pct"] == pytest.approx(-result["net_return_pct"], abs=1e-4)
    assert result["fee_schedule_version"] == FEE_SCHEDULE["version"]


def test_rejects_non_positive_notional():
    with pytest.raises(ValueError):
        net_return_pct(gross_return_pct=1.0, notional=0.0, asof=ASOF)


def test_rejects_total_loss_because_exit_value_is_not_positive():
    with pytest.raises(ValueError):
        net_return_pct(gross_return_pct=-100.0, notional=20_000.0, asof=ASOF)


def test_rejects_dates_before_the_fee_schedule_takes_effect():
    with pytest.raises(ValueError):
        net_return_pct(
            gross_return_pct=1.0, notional=20_000.0, asof="2020-01-01"
        )
