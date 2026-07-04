import pytest

import pullback_strategy
from scripts import pullback_gate_evaluation as evaluation


def _bar(date, close, *, open_=None, low=None, high=None):
    return {
        "date": date,
        "open": open_ if open_ is not None else close,
        "close": close,
        "high": high if high is not None else max(close, open_ or close) * 1.01,
        "low": low if low is not None else min(close, open_ or close) * 0.99,
        "volume": 1_000_000,
    }


def _uptrend_pullback_bars():
    """35 根：稳步主升 → 回调至 MA10 → 末根收阳企稳。"""
    bars = []
    price = 10.0
    for i in range(30):
        price *= 1.016
        bars.append(_bar(f"2026-06-{i + 1:02d}", round(price, 3)))
    peak = price
    for i in range(4):
        price *= 0.985
        bars.append(_bar(f"2026-07-{i + 1:02d}", round(price, 3)))
    stabilize = round(price * 1.005, 3)
    bars.append(_bar(
        "2026-07-05", stabilize,
        open_=round(price * 0.998, 3),
        low=round(price * 0.985, 3),
    ))
    assert stabilize < peak
    return bars


def test_signal_fires_on_stabilized_pullback_in_uptrend():
    bars = _uptrend_pullback_bars()
    assert pullback_strategy.signal_on_last_bar(bars) is True
    result = pullback_strategy.analyze(bars)
    assert result["signals"][0]["strategy_id"] == "rs_leader_pullback"
    assert result["signals"][0]["idx"] == len(bars) - 1


def test_no_signal_without_leadership_momentum():
    bars = [
        _bar(f"2026-06-{i + 1:02d}", 10.0 + 0.01 * i) for i in range(34)
    ]
    bars.append(_bar("2026-07-05", 10.36, open_=10.30, low=10.20))
    assert pullback_strategy.signal_on_last_bar(bars) is False


def test_no_signal_when_still_falling():
    bars = _uptrend_pullback_bars()
    last = dict(bars[-1])
    last["close"] = round(float(bars[-2]["close"]) * 0.97, 3)
    last["open"] = round(last["close"] * 1.01, 3)
    bars[-1] = last
    assert pullback_strategy.signal_on_last_bar(bars) is False


def test_no_signal_when_pullback_too_deep():
    bars = _uptrend_pullback_bars()[:-1]
    price = float(bars[-1]["close"])
    for i in range(6):
        price *= 0.96
        bars.append(_bar(f"2026-07-{i + 6:02d}", round(price, 3)))
    bars.append(_bar("2026-07-12", round(price * 1.004, 3),
                     open_=round(price * 0.998, 3)))
    assert pullback_strategy.signal_on_last_bar(bars) is False


def test_no_signal_on_short_history_or_dirty_bars():
    assert pullback_strategy.signal_on_last_bar([_bar("2026-07-01", 10)] * 5) is False
    bars = _uptrend_pullback_bars()
    bars[-3] = {**bars[-3], "close": "not-a-number"}
    assert pullback_strategy.signal_on_last_bar(bars) is False


def test_strategy_direction_registered_for_harness():
    import chan_signal_backtest as backtest

    assert backtest.STRATEGY_DIRECTIONS["rs_leader_pullback"] == "bullish"


@pytest.mark.parametrize("mode", ["synthetic"])
def test_synthetic_mode_never_emits_ab_verdict(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    result = evaluation.run_evaluation(
        mode=mode,
        split_date="2025-07-01",
        start_date="2019-11-01",
        n_perm=50,
        min_oos_samples=5,
        persist=False,
    )
    summary = evaluation.summarize_for_report(result)
    assert summary["verdict"] == "pending_real_data_run"
    assert summary["data_mode"] == "synthetic"
