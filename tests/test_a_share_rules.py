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


# ---------------------------------------------------------------------------
# P5(b) rule_version — 按日期取制度的断点测试
# 每条断点必须在「生效前一交易日」与「生效日」两侧各断言一次，否则改错日期
# 也能全绿（见 rules/testing.md 的 mutation check）。
# ---------------------------------------------------------------------------
def test_chinext_limit_breakpoint_2020_08_24():
    before = rules.price_limit_rule(code="300123", asof="2020-08-21")
    on_day = rules.price_limit_rule(code="300123", asof="2020-08-24")

    assert before["limit_pct"] == 10.0
    assert before["rule_id"] == "chinext_10pct"
    assert on_day["limit_pct"] == 20.0
    assert on_day["rule_id"] == "chinext_20pct"
    assert on_day["board"] == "chinext" and on_day["exchange"] == "SZSE"


def test_sse_main_risk_warning_breakpoint_2026_07_06():
    before = rules.price_limit_rule(code="600001", asof="2026-07-03", is_st=True)
    on_day = rules.price_limit_rule(code="600001", asof="2026-07-06", is_st=True)

    assert before["limit_pct"] == 5.0
    assert before["rule_id"] == "sse_main_risk_warning_5pct"
    assert on_day["limit_pct"] == 10.0
    assert on_day["rule_id"] == "sse_main_risk_warning_10pct"
    # 非风险警示的沪主板不受该断点影响，两侧都是 10%。
    assert rules.price_limit_pct_on("600001", "2026-07-03") == 10.0


def test_star_market_is_twenty_percent_and_blocked_before_open():
    assert rules.price_limit_pct_on("688001", "2019-07-22") == 20.0
    assert rules.price_limit_pct_on("688001", "2026-08-25") == 20.0
    blocked = rules.price_limit_rule(code="688001", asof="2019-07-01")
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "star_before_open"
    assert blocked["limit_pct"] is None


def test_bse_thirty_percent_breakpoint_2021_11_15():
    assert rules.price_limit_pct_on("830799", "2021-11-15") == 30.0
    assert rules.price_limit_rule(code="830799", asof="2021-11-12")["status"] == "blocked"


def test_registration_new_share_window_has_no_daily_limit():
    first_day = rules.price_limit_rule(
        code="301001", asof="2026-08-25", sessions_since_listing=1
    )
    fifth_day = rules.price_limit_rule(
        code="301001", asof="2026-08-25", sessions_since_listing=5
    )
    sixth_day = rules.price_limit_rule(
        code="301001", asof="2026-08-25", sessions_since_listing=6
    )

    assert first_day["status"] == "known" and first_day["limit_pct"] is None
    assert first_day["rule_id"] == "registration_new_share_no_limit"
    assert fifth_day["limit_pct"] is None
    assert sixth_day["limit_pct"] == 20.0          # 特殊期结束回到板块常规涨跌幅
    # 注册制之前的创业板新股没有这个 5 日免限期。
    legacy = rules.price_limit_rule(
        code="300123", asof="2019-05-06", sessions_since_listing=1
    )
    assert legacy["limit_pct"] == 10.0


def test_unknown_board_and_bad_date_fail_closed():
    assert rules.price_limit_rule(code="999999", asof="2026-08-25")["status"] == "blocked"
    assert rules.price_limit_rule(code="600001", asof="not-a-date")["status"] == "blocked"
    assert rules.price_limit_pct_on("600001", None) is None
