from skills.common.trend_state import TrendState, classify


def test_strong_positive_trend_is_trending_up():
    result = classify({
        "trend_efficiency": 0.8,
        "ma20_slope": 0.03,
        "ma60_slope": 0.02,
        "industry_residual_momentum": 0.08,
        "breakout_persistence": 5,
        "volume_vwap_confirmation": True,
        "short_term_inertia": 0.04,
    })
    assert result["state"] is TrendState.TRENDING_UP
    assert 0.0 <= result["confidence"] <= 1.0


def test_low_signal_to_noise_is_sideways():
    result = classify({
        "trend_efficiency": 0.15,
        "ma20_slope": 0.0,
        "ma60_slope": 0.001,
        "industry_residual_momentum": 0.0,
        "short_term_inertia": 0.0,
    })
    assert result["state"] is TrendState.SIDEWAYS


def test_positive_long_trend_with_negative_inertia_is_reversing():
    result = classify({
        "trend_efficiency": 0.8,
        "ma20_slope": 0.03,
        "ma60_slope": 0.02,
        "industry_residual_momentum": 0.08,
        "breakout_persistence": 5,
        "volume_vwap_confirmation": True,
        "short_term_inertia": -0.08,
    })
    assert result["state"] is TrendState.REVERSING
