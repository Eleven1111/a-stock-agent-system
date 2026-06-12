from datetime import date

import a_share_rules as rules


def test_2026_exchange_holiday_is_not_a_trading_day():
    assert rules.is_trading_day(date(2026, 6, 19)) is False
    assert rules.next_trading_day(date(2026, 6, 18)) == date(2026, 6, 22)


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

