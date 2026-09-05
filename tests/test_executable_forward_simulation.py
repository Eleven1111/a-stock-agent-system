"""A price-path label and an executable result are different measurements.

The settled forward label exits at the close of the session it entered on when
the horizon is 1.  These tests hold the line that such a label can never be
offered as evidence that a strategy is tradeable, and that the executable path
measures T+1 from the session the buy actually happened on.
"""

from __future__ import annotations

import pytest

from executable_forward_simulation import (
    STATUS_EXITED,
    STATUS_NOT_FILLED,
    STATUS_PENDING,
    STATUS_UNRESOLVED,
    simulate_executable_forward,
)
from forward_label_taxonomy import (
    LABEL_EXECUTABLE,
    LABEL_MANUAL_FILL,
    LABEL_PRICE_PATH,
    LabelKindError,
    assert_execution_evidence,
    describe_price_path_label,
)

POLICY = {"entry_rule": "next_trading_session_open_reference", "horizons": [1, 3]}


def _prediction(**overrides):
    record = {
        "decision_id": "d1",
        "strategy_id": "rank_surprise",
        "entity_id": "600000",
        "decision_date": "2026-09-01",
        "observed_at": "2026-09-01T20:00:00+08:00",
    }
    record.update(overrides)
    return record


def _bar(day: str, *, open_: float, high: float, low: float, close: float, amount: float = 5e8):
    return {
        "trading_date": day, "open": open_, "high": high, "low": low,
        "close": close, "amount": amount,
    }


def _normal_bars(days, base=10.0):
    return [
        _bar(day, open_=base + index * 0.1, high=base + 0.4 + index * 0.1,
             low=base - 0.3 + index * 0.1, close=base + 0.2 + index * 0.1)
        for index, day in enumerate(days)
    ]


SESSIONS = ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07"]


def test_a_price_path_label_states_its_clock_and_admits_it_breaks_t_plus_one():
    descriptor = describe_price_path_label(
        {
            "decision_date": "2026-09-01",
            "decision_available_at": "2026-09-01T20:00:00+08:00",
            "entry_date": "2026-09-02",
            "horizon_sessions": 1,
        },
        POLICY,
    )

    assert descriptor["label_kind"] == LABEL_PRICE_PATH
    assert descriptor["signal_cutoff"] == "2026-09-01"
    assert descriptor["signal_available_at"] == "2026-09-01T20:00:00+08:00"
    assert descriptor["earliest_executable_entry"] == "2026-09-02"
    assert descriptor["exit_rule"] == "close_of_session_1_after_reference_entry"
    assert descriptor["respects_t_plus_one_from_entry"] is False
    assert descriptor["cost_basis"] == "modelled_assumption"
    assert descriptor["execution_evidence"] is False


def test_a_three_session_label_does_respect_t_plus_one():
    descriptor = describe_price_path_label(
        {"decision_date": "2026-09-01", "entry_date": "2026-09-02", "horizon_sessions": 3},
        POLICY,
    )
    assert descriptor["respects_t_plus_one_from_entry"] is True


def test_a_price_path_label_cannot_pass_an_execution_gate():
    descriptor = describe_price_path_label(
        {"decision_date": "2026-09-01", "entry_date": "2026-09-02", "horizon_sessions": 1},
        POLICY,
    )
    with pytest.raises(LabelKindError, match="price_path_label_is_not_execution_evidence"):
        assert_execution_evidence(descriptor)


def test_an_execution_gate_rejects_unknown_and_unflagged_labels():
    with pytest.raises(LabelKindError, match="unknown_label_kind"):
        assert_execution_evidence({"label_kind": "looks_official"})
    with pytest.raises(LabelKindError, match="missing_execution_evidence_flag"):
        assert_execution_evidence({"label_kind": LABEL_EXECUTABLE})
    assert_execution_evidence({"label_kind": LABEL_EXECUTABLE, "execution_evidence": True})
    assert_execution_evidence({"label_kind": LABEL_MANUAL_FILL})


def test_the_executable_path_never_sells_on_the_session_it_bought():
    result = simulate_executable_forward(
        _prediction(), _normal_bars(SESSIONS), order_amount=20000.0, prev_close=9.9
    )

    assert result["status"] == STATUS_EXITED
    assert result["entry_date"] == "2026-09-02"
    assert result["exit_date"] == "2026-09-03"
    assert result["exit_date"] > result["entry_date"]
    assert result["sessions_held"] == 1
    assert result["respects_t_plus_one_from_entry"] is True
    assert result["label_kind"] == LABEL_EXECUTABLE
    assert result["execution_evidence"] is True
    assert_execution_evidence(result)


def test_a_same_session_exit_is_refused_outright():
    with pytest.raises(ValueError, match="same-session exit"):
        simulate_executable_forward(_prediction(), _normal_bars(SESSIONS), hold_sessions=0)


def test_a_one_word_limit_up_entry_is_recorded_as_unfilled_not_dropped():
    bars = [
        _bar("2026-09-02", open_=11.0, high=11.0, low=11.0, close=11.0),
        *_normal_bars(SESSIONS[1:], base=11.0),
    ]
    result = simulate_executable_forward(
        _prediction(), bars, order_amount=20000.0, prev_close=10.0
    )

    assert result["status"] == STATUS_NOT_FILLED
    assert result["reason"] == "one_word_limit_up_no_fill"
    assert result["entry_date"] == "2026-09-02"
    # Still a row: the denominator keeps the days the strategy could not act on.
    assert "entry_assessment" in result


def test_a_limit_down_exit_defers_to_the_next_sellable_session():
    bars = [
        _bar("2026-09-02", open_=10.0, high=10.3, low=9.9, close=10.2),
        _bar("2026-09-03", open_=9.18, high=9.18, low=9.18, close=9.18, amount=1e5),
        _bar("2026-09-04", open_=9.3, high=9.6, low=9.2, close=9.5),
    ]
    result = simulate_executable_forward(
        _prediction(), bars, order_amount=20000.0, prev_close=10.0
    )

    assert result["status"] == STATUS_EXITED
    assert result["exit_date"] == "2026-09-04"
    assert result["days_blocked"] == 1
    assert result["deferrals"][0]["session"] == "2026-09-03"
    assert result["deferrals"][0]["reason"] in {
        "one_word_limit_down_no_bid", "limit_down_insufficient_bid",
    }


def test_an_exit_that_never_clears_is_right_censored_rather_than_discarded():
    bars = [
        _bar("2026-09-02", open_=10.0, high=10.3, low=9.9, close=10.2),
        _bar("2026-09-03", open_=9.18, high=9.18, low=9.18, close=9.18, amount=1e5),
    ]
    result = simulate_executable_forward(
        _prediction(), bars, order_amount=20000.0, prev_close=10.0
    )

    assert result["status"] == STATUS_UNRESOLVED
    assert result["execution_evidence"] is False
    assert result["days_blocked"] == 1
    with pytest.raises(LabelKindError):
        assert_execution_evidence(result)


def test_a_signal_that_arrives_after_the_entry_session_is_not_backfilled():
    result = simulate_executable_forward(
        _prediction(observed_at="2026-09-02T10:00:00+08:00"),
        _normal_bars(SESSIONS), order_amount=20000.0, prev_close=9.9,
    )

    assert result["status"] == STATUS_PENDING
    assert result["reason"] == "signal_not_available_before_entry_session"


def test_missing_quotes_leave_the_sample_pending_instead_of_filled():
    assert simulate_executable_forward(_prediction(), [])["status"] == STATUS_PENDING
    stale = simulate_executable_forward(
        _prediction(), _normal_bars(["2026-08-28", "2026-08-31"])
    )
    assert stale["status"] == STATUS_PENDING


def test_rerunning_the_same_inputs_produces_the_same_result():
    first = simulate_executable_forward(
        _prediction(), _normal_bars(SESSIONS), order_amount=20000.0, prev_close=9.9
    )
    second = simulate_executable_forward(
        _prediction(), _normal_bars(SESSIONS), order_amount=20000.0, prev_close=9.9
    )
    assert first == second


def test_a_longer_hold_moves_the_exit_without_touching_the_entry():
    short = simulate_executable_forward(
        _prediction(), _normal_bars(SESSIONS), order_amount=20000.0, prev_close=9.9
    )
    longer = simulate_executable_forward(
        _prediction(), _normal_bars(SESSIONS), hold_sessions=3,
        order_amount=20000.0, prev_close=9.9,
    )

    assert longer["entry_date"] == short["entry_date"]
    assert longer["exit_date"] == "2026-09-07"
    assert longer["sessions_held"] == 3
