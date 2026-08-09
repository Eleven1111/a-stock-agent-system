from skills.common.trend_exit import DEFAULT_RULES, evaluate_hold


def _entry(**updates):
    value = {"price": 100.0, "atr": 2.0, "breakout_level": 98.0}
    value.update(updates)
    return value


def test_initial_atr_stop_triggers():
    result = evaluate_hold(_entry(), {"price": 94.0, "low": 94.0, "holding_days": 1})
    assert result["action"] == "exit"
    assert result["reason"] == "initial_atr_stop"


def test_trailing_stop_only_moves_up():
    first = evaluate_hold(_entry(), {"price": 110.0, "holding_days": 2})
    second = evaluate_hold(
        _entry(high_watermark=110.0, trailing_stop=104.0),
        {"price": 106.0, "holding_days": 3},
    )
    assert first["action"] == "hold"
    assert first["trailing_stop"] == 104.0
    assert second["trailing_stop"] == 104.0


def test_five_day_no_progress_time_stop():
    result = evaluate_hold(_entry(), {"price": 100.0, "holding_days": 5})
    assert result["action"] == "exit"
    assert result["reason"] == "time_stop_5d_no_progress"


def test_fallback_below_breakout_exits():
    result = evaluate_hold(_entry(), {"price": 98.0, "holding_days": 2})
    assert result["action"] == "exit"
    assert result["reason"] == "fell_back_below_breakout"


def test_default_rules_are_registered_initial_values():
    assert 2.0 <= DEFAULT_RULES["initial_atr_multiple"] <= 3.0
    assert DEFAULT_RULES["time_stop_days"] in (5, 10)
