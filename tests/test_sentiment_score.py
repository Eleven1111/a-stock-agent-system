"""统一情绪评分 S_t（升级方案 P0-c）。

守四条：预热不足 fail-closed（不给 50 分）、炸板率是**反向**分量、分量降级后权重
不足即整体不可用、冰点谓词四条件缺一不触发。所有权重/窗口来自 config/scoring.yaml。
"""

import pytest

import sentiment_score as ss


def series(days, **overrides):
    """构造 ``days`` 条升序记录；overrides 按字段给出末日的覆盖值。"""
    rows = []
    for index in range(days):
        rows.append({
            "trading_date": f"2026-01-{index + 1:03d}",
            "limit_premium_close": 1.0 + index * 0.01,
            "limit_count": 30 + index % 7,
            "adr": 1.0 + index % 5 * 0.1,
            "break_rate": 0.3 + index % 3 * 0.05,
            "max_board": 4 + index % 3,
            "board4plus": 2 + index % 4,
            "leader_damage": -1.0 + index % 5 * 0.5,
        })
    rows[-1].update(overrides)
    return rows


@pytest.fixture
def config():
    """真实配置——不是测试自造的一份影子参数。"""
    loaded = ss.load_config()
    assert loaded is not None, "config/scoring.yaml 缺 sentiment_score 节"
    return loaded


# --- 配置来源 -----------------------------------------------------------


def test_weights_and_windows_come_from_the_repository_config(config):
    assert int(config["quantile_window"]) == 120
    assert int(config["min_history"]) == 180
    weights = [float(spec["weight"]) for spec in config["components"].values()]
    assert sum(weights) == pytest.approx(1.0)


def test_missing_config_section_fails_closed():
    """本模块内没有等价的数字默认值：配置缺失即不可用。"""
    assert ss.load_config({"scoring": {}}) is None
    result = ss.compute_sentiment_score(series(200), config={})
    assert result["status"] == "unavailable"
    assert result["reason"] == "config_missing"


# --- 预热 fail-closed ---------------------------------------------------


def test_insufficient_history_returns_unavailable_not_fifty(config):
    result = ss.compute_sentiment_score(series(179), config=config)
    assert result["status"] == "unavailable"
    assert result["reason"] == "insufficient_history"
    assert result["score"] is None
    assert result["observed_days"] == 179
    assert result["required_days"] == 180


def test_empty_series_is_unavailable(config):
    result = ss.compute_sentiment_score([], config=config)
    assert result["status"] == "unavailable"
    assert result["score"] is None


def test_full_history_scores_and_stays_uncalibrated(config):
    result = ss.compute_sentiment_score(series(200), config=config)
    assert result["status"] == "ok"
    assert 0.0 <= result["score"] <= 100.0
    assert result["calibrated"] is False
    assert result["band"] in {"冰点", "修复", "发酵", "加速", "极热"}


# --- 分量方向与降级 -----------------------------------------------------


def test_break_rate_is_an_inverse_component(config):
    """炸板率越高，情绪分越低。权重符号一旦改反，这条立刻变红。"""
    calm = ss.compute_sentiment_score(series(200, break_rate=0.05), config=config)
    panic = ss.compute_sentiment_score(series(200, break_rate=0.95), config=config)
    assert calm["score"] > panic["score"]
    assert calm["components"]["break_rate"]["inverted"] is True


def test_premium_is_a_direct_component(config):
    weak = ss.compute_sentiment_score(series(200, limit_premium_close=-9.0), config=config)
    strong = ss.compute_sentiment_score(series(200, limit_premium_close=99.0), config=config)
    assert strong["score"] > weak["score"]


def test_missing_component_degrades_and_renormalizes(config):
    """单个字段全窗口缺失 → 该分量剔除并归一化，其余分量照常给分。"""
    rows = [dict(row, board4plus=None) for row in series(200)]
    result = ss.compute_sentiment_score(rows, config=config)
    assert result["status"] == "ok"
    assert result["unavailable_components"] == ["board4plus"]
    assert result["available_weight"] == pytest.approx(0.90)


def test_too_many_missing_components_fail_closed(config):
    """可用权重跌破下限就整体不可用——用两三个分量拼出来的分不叫情绪分。"""
    blanked = {"limit_premium_close": None, "limit_count": None, "adr": None,
               "break_rate": None, "max_board": None}
    rows = [dict(row, **blanked) for row in series(200)]
    result = ss.compute_sentiment_score(rows, config=config)
    assert result["status"] == "unavailable"
    assert result["reason"] == "insufficient_component_weight"
    assert result["score"] is None


# --- ΔS / Δ²S -----------------------------------------------------------


def test_delta_and_second_delta_are_reported(config):
    result = ss.compute_sentiment_score(series(200, break_rate=0.02), config=config)
    assert result["delta"] == pytest.approx(
        result["score"] - result["previous_score"], abs=1e-4
    )
    assert result["delta_squared"] is not None


def test_rolling_quantile_counts_within_window():
    assert ss.rolling_quantile([1.0, 2.0, 3.0, 4.0], 3.0) == pytest.approx(0.75)
    assert ss.rolling_quantile([5.0], 5.0) == pytest.approx(1.0)


# --- 冰点确认谓词（四条件缺一不触发）------------------------------------


def _score(*, status="ok", previous=10.0, delta=15.0):
    return {"status": status, "previous_score": previous, "delta": delta}


def test_ice_confirm_requires_all_four_conditions(config):
    confirmed = ss.ice_point_confirmed(
        _score(), leader_confirm=True, sector_breadth_top=3, config=config
    )
    assert confirmed["confirmed"] is True
    assert confirmed["reasons"] == []
    assert confirmed["shadow_only"] is True


@pytest.mark.parametrize(
    "kwargs,expected_reason",
    [
        ({"score": _score(previous=25.0)}, "previous_score_not_extreme"),
        ({"score": _score(delta=3.0)}, "delta_below_threshold"),
        ({"leader_confirm": False}, "leader_not_confirmed"),
        ({"leader_confirm": None}, "leader_not_confirmed"),
        ({"sector_breadth_top": 2}, "sector_breadth_below_threshold"),
        ({"sector_breadth_top": None}, "sector_breadth_below_threshold"),
        ({"score": {"status": "unavailable"}}, "score_unavailable"),
    ],
)
def test_ice_confirm_fails_when_any_condition_is_missing(kwargs, expected_reason, config):
    """缺一不可，且"数据不可用"一律判否——缺证据不是满足条件。"""
    payload = {"score": _score(), "leader_confirm": True, "sector_breadth_top": 3}
    payload.update(kwargs)
    result = ss.ice_point_confirmed(
        payload["score"],
        leader_confirm=payload["leader_confirm"],
        sector_breadth_top=payload["sector_breadth_top"],
        config=config,
    )
    assert result["confirmed"] is False
    assert expected_reason in result["reasons"]


def test_ice_confirm_without_config_fails_closed():
    result = ss.ice_point_confirmed(_score(), leader_confirm=True,
                                    sector_breadth_top=5, config={})
    assert result["confirmed"] is False
    assert result["reasons"] == ["config_missing"]
