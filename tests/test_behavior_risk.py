"""Agent 行为漂移度量（纯函数，读 signal 序列，不触网）。"""

import behavior_risk


def _sig(date, outcome=None, pnl=None, strat="trend_pullback"):
    return {"signal_date": date, "outcome": outcome, "pnl_pct": pnl, "strategy_id": strat}


def test_win_streak_with_action_acceleration_flags_expansion():
    asof = "2026-06-22"
    sigs = [_sig(d, "win", 5.0) for d in ["2026-05-25", "2026-05-28"]] + [
        _sig(d, "win", 5.0)
        for d in ["2026-06-14", "2026-06-16", "2026-06-18", "2026-06-20", "2026-06-22"]
    ]
    r = behavior_risk.assess_behavior_risk(sigs, asof=asof)
    assert r["win_streak"] == 7
    assert r["streak_expansion"] is True
    assert any("连胜" in f for f in r["flags"])
    assert r["behavior_risk_score"] >= 0.4


def test_loss_streak_with_more_actions_flags_recovery_pressure():
    asof = "2026-06-22"
    sigs = [_sig("2026-05-26", "win", 3.0)] + [
        _sig(d, "loss", -3.0)
        for d in ["2026-06-15", "2026-06-17", "2026-06-19", "2026-06-21"]
    ]
    r = behavior_risk.assess_behavior_risk(sigs, asof=asof)
    assert r["loss_streak"] == 4
    assert r["loss_recovery_pressure"] is True
    assert any("翻本" in f for f in r["flags"])


def test_empty_signals_degrade_gracefully():
    r = behavior_risk.assess_behavior_risk([])
    assert r["action_rate_drift"] is None
    assert r["win_streak"] == 0 and r["loss_streak"] == 0
    assert r["behavior_risk_score"] == 0.0
    assert r["unavailable"] == ["one_sided_evidence", "horizon_drift"]


def test_tail_streak_counts_only_trailing_same_class():
    sigs = [
        _sig("2026-06-10", "win", 1.0),
        _sig("2026-06-11", "loss", -1.0),
        _sig("2026-06-12", "loss", -1.0),
    ]
    r = behavior_risk.assess_behavior_risk(sigs, asof="2026-06-12")
    assert r["loss_streak"] == 2
    assert r["win_streak"] == 0


def test_strategy_concentration_high_when_single_strategy():
    sigs = [_sig(d, "win", 1.0, strat="daban:x") for d in ["2026-06-18", "2026-06-19", "2026-06-20"]]
    r = behavior_risk.assess_behavior_risk(sigs, asof="2026-06-20")
    assert r["strategy_concentration_hhi"] == 1.0


def test_pending_signals_excluded_from_streak():
    sigs = [_sig("2026-06-18", "win", 2.0), _sig("2026-06-20", None, None)]
    r = behavior_risk.assess_behavior_risk(sigs, asof="2026-06-20")
    assert r["settled_count"] == 1
    assert r["win_streak"] == 1


def test_asof_excludes_future_signals_from_all_behavior_metrics():
    sigs = [
        _sig("2026-06-18", "loss", -2.0, strat="trend_pullback"),
        _sig("2026-06-19", "loss", -2.0, strat="trend_pullback"),
        _sig("2026-06-21", "win", 8.0, strat="future_strategy"),
    ]

    r = behavior_risk.assess_behavior_risk(sigs, asof="2026-06-20")

    assert r["signal_count"] == 2
    assert r["settled_count"] == 2
    assert r["loss_streak"] == 2
    assert r["win_streak"] == 0
