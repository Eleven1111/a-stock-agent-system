"""Config-gated theme-stage weighting hook in candidate_pipeline.rank_candidates."""

from __future__ import annotations

import candidate_pipeline as cp


def _kline():
    # 25 rising bars so feature_ready is True and daban is eligible.
    bars = []
    price = 10.0
    for _ in range(25):
        price *= 1.01
        bars.append({"open": price, "high": price * 1.01, "low": price * 0.99,
                     "close": price, "volume": 1_000_000})
    return bars


def _eligible():
    return [{"code": "600000", "name": "浦发银行", "amount": 5e8, "change_pct": 5.0,
             "sector": "机器人", "turnover": 4.0}]


def test_hook_noop_when_no_theme_map():
    ranked = cp.rank_candidates(_eligible(), {"600000": _kline()})
    assert ranked[0]["theme_stage_bonus"] == 0.0
    assert ranked[0]["theme_stage"] is None


def test_hook_noop_when_disabled():
    stages = {"机器人": {"id": "theme:机器人", "stage": "mainline"}}
    ranked = cp.rank_candidates(
        _eligible(), {"600000": _kline()},
        theme_stages=stages, theme_weighting={"enabled": False},
    )
    assert ranked[0]["theme_stage_bonus"] == 0.0


def test_mainline_adds_bonus():
    stages = {"机器人": {"id": "theme:机器人", "stage": "mainline"}}
    base = cp.rank_candidates(_eligible(), {"600000": _kline()})[0]
    boosted = cp.rank_candidates(
        _eligible(), {"600000": _kline()}, theme_stages=stages,
    )[0]
    assert boosted["theme_stage_bonus"] == 3.0
    assert boosted["theme_stage"] == "mainline"
    assert boosted["trend_score"] > base["trend_score"]


def test_fading_downweights():
    stages = {"机器人": {"id": "theme:机器人", "stage": "fading"}}
    base = cp.rank_candidates(_eligible(), {"600000": _kline()})[0]
    faded = cp.rank_candidates(
        _eligible(), {"600000": _kline()}, theme_stages=stages,
    )[0]
    assert faded["theme_stage_bonus"] == -6.0
    assert faded["trend_score"] < base["trend_score"]


def test_custom_stage_deltas_from_config():
    stages = {"机器人": {"id": "theme:机器人", "stage": "diverging"}}
    faded = cp.rank_candidates(
        _eligible(), {"600000": _kline()},
        theme_stages=stages,
        theme_weighting={"enabled": True, "stage_deltas": {"diverging": -10.0}},
    )[0]
    assert faded["theme_stage_bonus"] == -10.0


def test_unknown_sector_is_noop():
    stages = {"半导体": {"id": "theme:半导体", "stage": "mainline"}}
    ranked = cp.rank_candidates(
        _eligible(), {"600000": _kline()}, theme_stages=stages,
    )[0]
    assert ranked["theme_stage_bonus"] == 0.0
