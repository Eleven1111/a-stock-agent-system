from datetime import date

import a_share_rules as rules
import pytest


def test_2026_exchange_holiday_is_not_a_trading_day():
    assert rules.is_trading_day(date(2026, 6, 19)) is False
    assert rules.next_trading_day(date(2026, 6, 18)) == date(2026, 6, 22)
    assert rules.previous_trading_day(date(2026, 6, 22)) == date(2026, 6, 18)


def test_t1_constraint_blocks_same_day_sale():
    constraint = rules.t1_constraint(
        acquired_on=date(2026, 6, 12),
        asof=date(2026, 6, 12),
    )

    assert constraint["market"] == "A_SHARE"
    assert constraint["settlement_rule"] == "T+1"
    assert constraint["same_day_sell_allowed"] is False
    assert constraint["sell_allowed"] is False
    assert constraint["earliest_sell_date"] == "2026-06-15"


def test_t1_constraint_unlocks_on_next_trading_day():
    constraint = rules.t1_constraint(
        acquired_on=date(2026, 6, 12),
        asof=date(2026, 6, 15),
    )

    assert constraint["sell_allowed"] is True


def test_calendar_coverage_gaps_fail_closed():
    with pytest.raises(rules.CalendarCoverageError, match="2027"):
        rules.is_trading_day(date(2027, 1, 1))

    with pytest.raises(rules.CalendarCoverageError, match="2027"):
        rules.t1_constraint(
            acquired_on=date(2026, 12, 31),
            asof=date(2027, 1, 4),
        )

    with pytest.raises(rules.CalendarCoverageError, match="2025"):
        rules.next_trading_day(date(2025, 12, 31))

    with pytest.raises(rules.CalendarCoverageError, match="2025"):
        rules.t1_constraint(
            acquired_on=date(2025, 12, 31),
            asof=date(2026, 1, 5),
        )
