"""交易纪律熔断（纯函数，读 trade.executed 事件，不触网）。"""

import trading_discipline


def _trade(action, trade_date, pnl=None):
    return {
        "event_type": "trade.executed",
        "payload": {"action": action, "trade_date": trade_date, "pnl": pnl},
    }


def test_clean_history_is_not_blocked():
    r = trading_discipline.assess_discipline_state([], total_assets=100000, asof="2026-06-24")
    assert r["blocked"] is False
    assert r["reasons"] == []
    assert r["week_trades"] == 0
    assert r["day_loss_pct"] == 0.0
    assert r["week_loss_pct"] == 0.0
    assert r["consecutive_losses"] == 0


def test_week_trade_cap_blocks_on_third_open():
    # 2026-06-24 是周三；同周一/二/三各开一仓
    events = [_trade("open", d) for d in ["2026-06-22", "2026-06-23", "2026-06-24"]]
    r = trading_discipline.assess_discipline_state(events, total_assets=100000, asof="2026-06-24")
    assert r["week_trades"] == 3
    assert r["blocked"] is True
    assert "week_trade_cap" in r["reasons"]


def test_open_from_last_week_does_not_count():
    events = [_trade("open", "2026-06-19")]  # 上周五
    r = trading_discipline.assess_discipline_state(events, total_assets=100000, asof="2026-06-24")
    assert r["week_trades"] == 0
    assert r["blocked"] is False


def test_day_loss_stop_blocks():
    events = [_trade("close", "2026-06-24", pnl=-2500)]
    r = trading_discipline.assess_discipline_state(events, total_assets=100000, asof="2026-06-24")
    assert r["day_loss_pct"] == -2.5
    assert r["blocked"] is True
    assert "day_loss_stop" in r["reasons"]


def test_week_loss_freeze_blocks_without_tripping_day_stop():
    events = [
        _trade("close", "2026-06-22", pnl=-2000),
        _trade("close", "2026-06-23", pnl=-2000),
        _trade("close", "2026-06-24", pnl=-1500),
    ]
    r = trading_discipline.assess_discipline_state(events, total_assets=100000, asof="2026-06-24")
    assert r["week_loss_pct"] == -5.5
    assert r["day_loss_pct"] == -1.5
    assert "week_loss_freeze" in r["reasons"]
    assert "day_loss_stop" not in r["reasons"]


def test_consecutive_losses_freeze_counts_trailing_streak_only():
    events = [
        _trade("close", "2026-06-19", pnl=500),   # 盈利，不计入连续
        _trade("close", "2026-06-22", pnl=-100),
        _trade("close", "2026-06-23", pnl=-100),
        _trade("close", "2026-06-24", pnl=-100),
    ]
    r = trading_discipline.assess_discipline_state(events, total_assets=100000, asof="2026-06-24")
    assert r["consecutive_losses"] == 3
    assert "consecutive_losses_freeze" in r["reasons"]


def test_win_breaks_the_losing_streak():
    events = [
        _trade("close", "2026-06-22", pnl=-100),
        _trade("close", "2026-06-23", pnl=-100),
        _trade("close", "2026-06-24", pnl=100),
    ]
    r = trading_discipline.assess_discipline_state(events, total_assets=100000, asof="2026-06-24")
    assert r["consecutive_losses"] == 0
    assert r["blocked"] is False


def test_zero_total_assets_degrades_gracefully_without_crash():
    events = [_trade("close", "2026-06-24", pnl=-500)]
    r = trading_discipline.assess_discipline_state(events, total_assets=0, asof="2026-06-24")
    assert r["day_loss_pct"] == 0.0
    assert r["week_loss_pct"] == 0.0


def test_non_trade_events_are_ignored():
    events = [{"event_type": "signal.opened", "payload": {"action": "open", "trade_date": "2026-06-24"}}]
    r = trading_discipline.assess_discipline_state(events, total_assets=100000, asof="2026-06-24")
    assert r["week_trades"] == 0
