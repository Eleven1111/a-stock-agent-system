"""Theme strength dimensions, rolling RS, and lifecycle FSM (with replay)."""

from __future__ import annotations


import theme_strength as ts


# ── breadth ──────────────────────────────────────────────────────────────

def test_breadth_unavailable_without_member_quotes():
    out = ts.compute_breadth(["600000"], {}, {})
    assert out["status"] == "unavailable"
    assert out["reason"] == "no_member_quotes"


def test_breadth_counts_up_limitup_and_ladder():
    quotes = {
        "600000": {"change_pct": 9.9},
        "000001": {"change_pct": 3.0},
        "000002": {"change_pct": -1.0},
    }
    ladder = {"600000": {"lianban": 3}, "000001": {"lianban": 1}}
    out = ts.compute_breadth(["600000", "000001", "000002"], quotes, ladder)
    assert out["status"] == "ok"
    assert out["up_count"] == 2
    assert out["limit_up_count"] == 1
    assert out["ladder_height"] == 3
    assert out["up_ratio"] == round(2 / 3, 4)


# ── capital (fail-closed) ───────────────────────────────────────────────

def test_capital_unavailable_when_flows_absent():
    assert ts.compute_capital(["600000"], None)["status"] == "unavailable"
    assert ts.compute_capital(["600000"], {})["status"] == "unavailable"


def test_capital_unavailable_when_no_member_records():
    flows = {"999999": {"main_net_yi": 2.0}}
    assert ts.compute_capital(["600000"], flows)["status"] == "unavailable"


def test_capital_aggregates_members():
    flows = {"600000": {"main_net_yi": 2.0}, "000001": {"main_net_yi": -0.5}}
    out = ts.compute_capital(["600000", "000001"], flows)
    assert out["status"] == "ok"
    assert out["main_net_yi"] == 1.5
    assert out["members_with_flow"] == 2


# ── market median / RS (fail-closed, no index proxy) ─────────────────────

def test_market_median_requires_real_universe_basis():
    small = {str(i): {"change_pct": 1.0} for i in range(50)}
    assert ts.market_median_return(small) is None  # < 100 quotes => no basis
    large = {str(i): {"change_pct": float(i % 5 - 2)} for i in range(200)}
    assert ts.market_median_return(large) is not None


def test_daily_excess_unavailable_without_market_basis():
    quotes = {"600000": {"change_pct": 5.0}}
    out = ts.theme_daily_excess(["600000"], quotes, None)
    assert out["status"] == "unavailable"
    assert out["reason"] == "no_whole_a_basis"


def test_daily_excess_computes_equal_weight_minus_median():
    quotes = {"600000": {"change_pct": 4.0}, "000001": {"change_pct": 2.0}}
    out = ts.theme_daily_excess(["600000", "000001"], quotes, market_median=0.5)
    assert out["theme_return"] == 3.0
    assert out["daily_excess"] == 2.5


def test_rolling_rs_fails_window_closed_on_gap():
    # 5-day window with one missing day must be unavailable, not shortened.
    series = [1.0, 1.0, None, 1.0, 1.0]
    rs = ts.rolling_rs(series, windows=(5,))
    assert rs["rs_5d"]["status"] == "unavailable"


def test_rolling_rs_sums_present_window():
    series = [0.5, 0.5, 0.5, 0.5, 0.5]
    rs = ts.rolling_rs(series, windows=(5,))
    assert rs["rs_5d"]["status"] == "ok"
    assert rs["rs_5d"]["value"] == 2.5


def test_index_basis_labelled_and_separate():
    theme = {"id": "theme:x", "name": "x", "members": ["600000"]}
    quotes = {"600000": {"change_pct": 4.0}}
    record = ts.build_theme_record(
        theme,
        asof="2026-07-03",
        quotes_by_code=quotes,
        ladder={},
        stock_flows={},
        market_median=None,
        prior_excess_series=[],
        index_basis={"index": "000905", "excess": 1.2},
    )
    # whole-A RS is unavailable, index basis is present but clearly labelled.
    assert record["relative_strength"]["rs_5d"]["status"] == "unavailable"
    assert "not whole-A median" in record["relative_strength_index_basis"]["note"]


# ── lifecycle FSM ────────────────────────────────────────────────────────

def _record(*, ladder=0, rs5=None):
    rs = {"rs_5d": {"status": "ok", "value": rs5} if rs5 is not None else {"status": "unavailable"}}
    return {"breadth": {"ladder_height": ladder}, "relative_strength": rs}


def test_emerging_to_mainline_on_resonance():
    out = ts.decide_stage("emerging", _record(ladder=3, rs5=1.5), persistence=1,
                          prior_ladder_height=None, weak_streak=0)
    assert out["stage"] == "mainline"
    assert out["reason"] == "resonance_confirmed"


def test_emerging_stays_without_rs_or_ladder():
    out = ts.decide_stage("emerging", _record(ladder=1, rs5=1.0), persistence=0,
                          prior_ladder_height=None, weak_streak=0)
    assert out["stage"] == "emerging"


def test_mainline_to_diverging_on_negative_rs():
    out = ts.decide_stage("mainline", _record(ladder=3, rs5=-0.1), persistence=3,
                          prior_ladder_height=3, weak_streak=1)
    assert out["stage"] == "diverging"
    assert out["reason"] == "rs_turned_negative"


def test_mainline_to_diverging_on_ladder_collapse():
    out = ts.decide_stage("mainline", _record(ladder=1, rs5=1.0), persistence=3,
                          prior_ladder_height=4, weak_streak=0)
    assert out["stage"] == "diverging"
    assert out["reason"] == "ladder_collapsed"


def test_diverging_to_fading_after_weak_streak():
    out = ts.decide_stage("diverging", _record(ladder=2, rs5=-0.2), persistence=0,
                          prior_ladder_height=2, weak_streak=2)
    assert out["stage"] == "fading"


def test_fading_to_archived_single_directional():
    out = ts.decide_stage("fading", _record(ladder=0, rs5=None), persistence=0,
                          prior_ladder_height=1, weak_streak=3)
    assert out["stage"] == "archived"


def test_archived_is_tombstone():
    out = ts.decide_stage("archived", _record(ladder=5, rs5=5.0), persistence=9,
                          prior_ladder_height=1, weak_streak=0)
    assert out["stage"] == "archived"
    assert out["reason"] == "tombstone"


def test_fading_not_auto_promoted_on_recovery():
    # a recovering theme in fading is NOT auto-promoted (no dead-cat chase).
    out = ts.decide_stage("fading", _record(ladder=4, rs5=3.0), persistence=5,
                          prior_ladder_height=4, weak_streak=0)
    assert out["stage"] == "fading"
    assert out["reason"] == "fading_hold"


def test_lifecycle_replay_lag_within_two_days():
    """Replay a synthetic multi-day series: a theme peaks then retreats.
    Assert fading is reached within 2 trading days of the RS turning negative."""
    # RS turns negative on day index 3 (0-based). fading_weak_days default = 2.
    rs_series = [1.5, 1.2, 0.8, -0.3, -0.5, -0.4]
    ladders = [3, 3, 3, 3, 2, 1]
    stage = "emerging"
    weak_streak = 0
    prior_ladder = None
    stages: list[str] = []
    first_negative = None
    fading_day = None
    for day, (rs5, ladder) in enumerate(zip(rs_series, ladders)):
        rec = _record(ladder=ladder, rs5=rs5)
        is_weak = rs5 <= 0
        if is_weak and first_negative is None:
            first_negative = day
        weak_streak = weak_streak + 1 if is_weak else 0
        out = ts.decide_stage(stage, rec, persistence=day,
                              prior_ladder_height=prior_ladder, weak_streak=weak_streak)
        stage = out["stage"]
        stages.append(stage)
        if stage == "fading" and fading_day is None:
            fading_day = day
        prior_ladder = ladder
    assert "mainline" in stages
    assert fading_day is not None
    assert fading_day - first_negative <= 2


# ── persistence + history ────────────────────────────────────────────────

def test_persistence_counts_trailing_strong_days():
    assert ts.compute_persistence([True, False, True, True]) == 2
    assert ts.compute_persistence([False]) == 0
    assert ts.compute_persistence([]) == 0


def test_history_roundtrip_and_idempotent_per_day(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    import importlib

    importlib.reload(ts)
    rec1 = {"schema": ts.SCHEMA, "theme_id": "theme:x", "asof": "2026-07-01",
            "is_strong": True, "daily_excess": {"status": "ok", "daily_excess": 1.0}}
    ts.append_history("theme:x", rec1)
    # re-run same day overwrites, not duplicates
    ts.append_history("theme:x", {**rec1, "is_strong": False})
    hist = ts.theme_history("theme:x")
    assert len(hist) == 1
    assert hist[0]["is_strong"] is False
    assert ts.prior_strong_flags("theme:x") == [False]
