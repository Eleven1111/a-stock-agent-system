import heuristic_research


def test_ablation_replays_factor_on_and_off_with_zero_live_effect():
    rows = [
        {"event_id": "e1", "session": "2026-07-01", "factor": 1.0, "return": 0.03},
        {"event_id": "e2", "session": "2026-07-02", "factor": 0.0, "return": -0.01},
    ]
    artifact = heuristic_research.run_ablation(
        "lhb_climax", rows, threshold=0.5, generated_at="2026-07-10T10:00:00+08:00"
    )
    assert artifact["schema"] == "heuristic_ablation_v1"
    assert artifact["live_effect"] == "none"
    assert artifact["factor_on"]["event_ids"] == ["e1"]
    assert artifact["factor_off"]["event_ids"] == ["e2"]
    assert len(artifact["input_sha256"]) == 64
    assert len(artifact["artifact_sha256"]) == 64


def test_lhb_hint_uses_trading_sessions_and_is_review_only():
    hint = heuristic_research.lhb_review_hint(
        signal_session="2026-07-03",
        asof_session="2026-07-07",
        trading_sessions=["2026-07-03", "2026-07-06", "2026-07-07"],
        max_holding_sessions=2,
    )
    assert hint["holding_sessions"] == 2
    assert hint["action"] == "review"
    assert hint["live_effect"] == "none"
