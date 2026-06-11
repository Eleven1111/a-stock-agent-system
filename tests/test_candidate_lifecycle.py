"""Candidate lifecycle persistence tests."""

import candidate_lifecycle as lifecycle


def test_lifecycle_preserves_stage_history_and_rejection_reasons(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    candidates = [
        {
            "code": "600001",
            "name": "入选股",
            "daban_score": 88.0,
            "trend_score": 61.0,
            "selected_by": {"daban": True, "trend": False},
        },
        {
            "code": "600002",
            "name": "淘汰股",
            "daban_score": 50.0,
            "trend_score": 55.0,
            "selected_by": {"daban": False, "trend": False},
        },
    ]

    lifecycle.initialize_day(
        "2026-06-10",
        candidates,
        rejected={"600003": ["成交额不足"]},
        metadata={"scanned_count": 3},
    )
    lifecycle.transition(
        "2026-06-10",
        stage="auction_shortlist",
        selected_codes={"600001"},
        rejection_reasons={"600002": ["竞价量不足"]},
        event_asof="2026-06-11",
    )

    state = lifecycle.load_day("2026-06-10")
    by_code = {item["code"]: item for item in state["records"]}

    assert state["metadata"]["scanned_count"] == 3
    assert by_code["600001"]["current_stage"] == "auction_shortlist"
    assert by_code["600001"]["stage_history"][-1]["selected"] is True
    assert by_code["600002"]["stage_history"][-1]["selected"] is False
    assert by_code["600002"]["rejection_reasons"] == ["竞价量不足"]
    assert by_code["600003"]["rejection_reasons"] == ["成交额不足"]
    assert len(by_code["600003"]["stage_history"]) == 1


def test_transition_normalizes_market_prefixed_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    lifecycle.initialize_day(
        "2026-06-10",
        [{"code": "600001", "name": "测试股", "selected_by": {"daban": True}}],
    )

    lifecycle.transition(
        "2026-06-10",
        stage="open_confirmed",
        selected_codes={"sh600001"},
        event_asof="2026-06-11",
    )

    record = lifecycle.load_day("2026-06-10")["records"][0]
    assert record["current_stage"] == "open_confirmed"
    assert record["stage_history"][-1]["selected"] is True


def test_settle_day_records_t1_and_t3_outcomes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    lifecycle.initialize_day(
        "2026-06-10",
        [{"code": "600001", "name": "测试股", "selected_by": {"trend": True}}],
    )
    klines = {
        "600001": [
            {"date": "2026-06-10", "open": 9.8, "close": 10.0, "high": 10.1, "low": 9.7},
            {"date": "2026-06-11", "open": 10.2, "close": 10.5, "high": 10.7, "low": 10.1},
            {"date": "2026-06-12", "open": 10.4, "close": 10.8, "high": 11.0, "low": 10.3},
            {"date": "2026-06-15", "open": 10.9, "close": 11.0, "high": 11.2, "low": 10.7},
        ]
    }

    lifecycle.settle_day("2026-06-10", klines)

    outcome = lifecycle.load_day("2026-06-10")["records"][0]["outcome"]
    assert outcome["resolved"] is True
    assert outcome["t1_open_ret"] == 2.0
    assert outcome["t1_close_ret"] == 5.0
    assert outcome["t3_close_ret"] == 10.0


def test_observe_day_uses_full_market_snapshots_for_t1_and_t3(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    lifecycle.initialize_day(
        "2026-06-10",
        [{
            "code": "600001",
            "name": "测试股",
            "price": 10.0,
            "selected_by": {"trend": True},
        }],
    )

    lifecycle.observe_day(
        "2026-06-10",
        "2026-06-11",
        1,
        {"sh600001": {"open": 10.2, "price": 10.5, "high": 10.7, "low": 10.1}},
    )
    lifecycle.observe_day(
        "2026-06-10",
        "2026-06-15",
        3,
        {"600001": {"open": 10.9, "price": 11.0, "high": 11.2, "low": 10.7}},
    )

    outcome = lifecycle.load_day("2026-06-10")["records"][0]["outcome"]
    assert outcome["resolved"] is True
    assert outcome["t1_open_ret"] == 2.0
    assert outcome["t1_close_ret"] == 5.0
    assert outcome["t3_close_ret"] == 10.0
    assert outcome["max_gain"] == 12.0
