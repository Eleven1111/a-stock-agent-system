import pytest

from tail_close_strategy import (
    AFTER_HOURS_STRATEGY_ID,
    canonical_hash,
    label_d1_outcome,
    simulate_after_hours_fixed_fill,
    simulate_continuous_fill,
)
from tail_close_test_support import TRADING_DATE, config


def _signal():
    cfg = config()
    return {
        "signal_id": "tail-test-1",
        "strategy_id": "tail_close:mainline_continuation_v1",
        "trading_date": TRADING_DATE,
        "reference_price": 10.0,
        "decision_mode": "replay",
        "snapshot_id": "snapshot-1",
        "snapshot_hash": "a" * 64,
        "config_hash": canonical_hash(cfg),
        "code_version": "test-commit",
        "requested_capacity": 100_000,
        "portfolio_allocation": {"allocated_capacity": 100_000},
        "research_only": True,
        "live_weight": 0,
    }


def test_continuous_fill_uses_only_post_arrival_visible_volume():
    bars = [
        {
            "event_time": f"{TRADING_DATE}T14:50:09+08:00",
            "available_time": f"{TRADING_DATE}T14:50:09+08:00",
            "ask_price": 9.99,
            "available_sell_volume": 200_000,
        },
        {
            "event_time": f"{TRADING_DATE}T14:50:11+08:00",
            "available_time": f"{TRADING_DATE}T14:50:11+08:00",
            "ask_price": 10.01,
            "available_sell_volume": 20_000,
        },
        {
            "event_time": f"{TRADING_DATE}T14:56:31+08:00",
            "available_time": f"{TRADING_DATE}T14:56:31+08:00",
            "ask_price": 10.01,
            "available_sell_volume": 200_000,
        },
    ]

    fill = simulate_continuous_fill(
        _signal(),
        bars,
        config(),
        decision_emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
    )

    assert fill["status"] == "PARTIAL_FILL"
    assert fill["filled_quantity"] == 500
    assert fill["unfilled_quantity"] == 9500
    assert [item["event_time"] for item in fill["fills"]] == [
        f"{TRADING_DATE}T14:50:11+08:00"
    ]
    assert fill["broker_called"] is False


def test_unfilled_signal_has_no_position_or_return():
    fill = simulate_continuous_fill(
        _signal(),
        [],
        config(),
        decision_emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
    )
    outcome = label_d1_outcome(fill, [], config())

    assert fill["status"] == "UNFILLED"
    assert outcome["status"] == "not_opened"
    assert outcome["capital_days"] == 0


def test_continuous_fill_is_hard_capped_by_portfolio_allocation():
    signal = _signal()
    signal["portfolio_allocation"]["allocated_capacity"] = 25_000
    fill = simulate_continuous_fill(
        signal,
        [
            {
                "event_time": f"{TRADING_DATE}T14:50:11+08:00",
                "available_time": f"{TRADING_DATE}T14:50:11+08:00",
                "ask_price": 10.0,
                "available_sell_volume": 1_000_000,
            }
        ],
        config(),
        decision_emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
        requested_notional=100_000,
    )

    assert fill["portfolio_allocated_capacity"] == 25_000
    assert fill["requested_notional"] == 25_000
    assert fill["requested_quantity"] == 2_500
    assert fill["filled_quantity"] == 2_500


def test_continuous_fill_rejects_missing_allocation_and_cross_date_bar():
    signal = _signal()
    signal.pop("portfolio_allocation")
    with pytest.raises(ValueError, match="portfolio_allocation_missing"):
        simulate_continuous_fill(
            signal,
            [],
            config(),
            decision_emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
        )

    with pytest.raises(ValueError, match="fill_bar_trading_date_mismatch"):
        simulate_continuous_fill(
            _signal(),
            [
                {
                    "time": "14:50",
                    "event_time": "2026-07-27T14:50:11+08:00",
                    "available_time": "2026-07-27T14:50:11+08:00",
                    "ask_price": 10.0,
                    "available_sell_volume": 1_000_000,
                }
            ],
            config(),
            decision_emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
        )


def test_fill_is_order_independent_and_ignores_data_visible_after_cancel():
    bars = [
        {
            "event_time": f"{TRADING_DATE}T14:50:12+08:00",
            "available_time": f"{TRADING_DATE}T14:50:12+08:00",
            "ask_price": 10.02,
            "available_sell_volume": 200_000,
        },
        {
            "event_time": f"{TRADING_DATE}T14:50:11+08:00",
            "available_time": f"{TRADING_DATE}T14:50:11+08:00",
            "ask_price": 10.00,
            "available_sell_volume": 200_000,
        },
        {
            "event_time": f"{TRADING_DATE}T14:50:13+08:00",
            "available_time": f"{TRADING_DATE}T15:10:00+08:00",
            "ask_price": 9.90,
            "available_sell_volume": 1_000_000,
        },
    ]

    first = simulate_continuous_fill(
        _signal(),
        bars,
        config(),
        decision_emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
    )
    second = simulate_continuous_fill(
        _signal(),
        list(reversed(bars)),
        config(),
        decision_emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
    )

    assert first["fill_hash"] == second["fill_hash"]
    assert first["fill_price"] == 10.01


def test_d1_blocked_exit_is_kept_and_right_censored():
    fill = {
        "signal_id": "tail-test-1",
        "strategy_id": "tail_close:mainline_continuation_v1",
        "trading_date": TRADING_DATE,
        "status": "FULL_FILL",
        "filled_quantity": 1000,
        "fill_price": 10.0,
    }
    sessions = [
        {
            "trading_date": day,
            "mark_price": 9.5 - offset * 0.2,
            "bars": [
                {
                    "time": "09:36",
                    "event_time": f"{day}T09:36:00+08:00",
                    "available_time": f"{day}T09:36:01+08:00",
                    "price": 9.5,
                    "volume": 10_000,
                    "blocked": True,
                }
            ],
        }
        for offset, day in enumerate(
            ["2026-07-29", "2026-07-30", "2026-07-31", "2026-08-03", "2026-08-04"]
        )
    ]

    outcome = label_d1_outcome(fill, sessions, config())

    assert outcome["status"] == "right_censored"
    assert outcome["days_blocked"] == 5
    assert outcome["remaining_quantity"] == 1000
    assert outcome["right_censored"] is True
    assert outcome["capital_days"] == 6


def test_d1_block_does_not_censor_before_five_session_window_finishes():
    fill = {
        "signal_id": "tail-test-1",
        "strategy_id": "tail_close:mainline_continuation_v1",
        "trading_date": TRADING_DATE,
        "status": "FULL_FILL",
        "filled_quantity": 1000,
        "fill_price": 10.0,
    }
    outcome = label_d1_outcome(
        fill,
        [
            {
                "trading_date": "2026-07-29",
                "mark_price": 9.5,
                "bars": [
                    {
                        "time": "09:36",
                        "event_time": "2026-07-29T09:36:00+08:00",
                        "available_time": "2026-07-29T09:36:01+08:00",
                        "blocked": True,
                    }
                ],
            }
        ],
        config(),
    )

    assert outcome["status"] == "blocked_pending"
    assert outcome["right_censored"] is False
    assert outcome["observation_complete"] is False


def test_d1_twap_splits_quantity_and_excludes_window_end_bar():
    fill = {
        "signal_id": "tail-test-1",
        "strategy_id": "tail_close:mainline_continuation_v1",
        "trading_date": TRADING_DATE,
        "status": "FULL_FILL",
        "filled_quantity": 1000,
        "fill_price": 10.0,
    }
    sessions = [
        {
            "trading_date": "2026-07-29",
            "mark_price": 10.0,
            "bars": [
                {
                    "time": clock,
                    "event_time": f"2026-07-29T{clock}:00+08:00",
                    "available_time": f"2026-07-29T{clock}:01+08:00",
                    "bid_price": price,
                    "available_buy_volume": 10_000,
                }
                for clock, price in [
                    ("09:35", 9.9),
                    ("09:36", 10.0),
                    ("09:37", 10.1),
                    ("09:38", 10.2),
                    ("09:39", 10.3),
                    ("09:40", 99.0),
                ]
            ],
        }
    ]

    outcome = label_d1_outcome(fill, sessions, config())

    assert outcome["status"] == "exited"
    assert [item["quantity"] for item in outcome["exit_fills"]] == [200] * 5
    assert outcome["exit_price"] == 10.1
    assert all("T09:40:" not in item["event_time"] for item in outcome["exit_fills"])
    assert outcome["exited_quantity"] + outcome["remaining_quantity"] == 1000


def test_d1_rejects_cross_date_or_single_time_bar():
    fill = {
        "signal_id": "tail-test-1",
        "strategy_id": "tail_close:mainline_continuation_v1",
        "trading_date": TRADING_DATE,
        "status": "FULL_FILL",
        "filled_quantity": 1000,
        "fill_price": 10.0,
    }
    sessions = [
        {
            "trading_date": "2026-07-29",
            "bars": [
                {
                    "time": "09:36",
                    "event_time": "2026-07-28T09:36:00+08:00",
                    "available_time": "2026-07-28T09:36:01+08:00",
                    "price": 10.0,
                    "volume": 1000,
                }
            ],
        }
    ]

    with pytest.raises(ValueError, match="exit_bar_trading_date_mismatch"):
        label_d1_outcome(fill, sessions, config())

    sessions[0]["bars"][0] = {"time": "09:36", "price": 10.0, "volume": 1000}
    with pytest.raises(ValueError, match="exit_bar_dual_time_incomplete"):
        label_d1_outcome(fill, sessions, config())


def test_after_hours_sibling_requires_observable_forward_queue():
    cfg = config()
    signal = {
        "strategy_id": AFTER_HOURS_STRATEGY_ID,
        "trading_date": TRADING_DATE,
        "close_price": 10.0,
        "requested_notional": 100_000,
        "queue_observable": False,
    }

    result = simulate_after_hours_fixed_fill(signal, [], cfg)

    assert result["status"] == "not_ready"
    assert result["reason"] == "queue_not_observable"
    assert result["broker_called"] is False


def test_after_hours_queue_model_is_forward_only_and_independent():
    cfg = config()
    signal = {
        "strategy_id": AFTER_HOURS_STRATEGY_ID,
        "trading_date": TRADING_DATE,
        "close_price": 10.0,
        "requested_notional": 100_000,
        "queue_observable": True,
    }
    observations = [
        {
            "event_time": f"{TRADING_DATE}T15:04:59+08:00",
            "available_time": f"{TRADING_DATE}T15:05:01+08:00",
            "incremental_matched_sell_volume": 1_000_000,
        },
        {
            "event_time": f"{TRADING_DATE}T15:06:00+08:00",
            "available_time": f"{TRADING_DATE}T15:06:01+08:00",
            "incremental_matched_sell_volume": 20_000,
        },
    ]

    result = simulate_after_hours_fixed_fill(signal, observations, cfg)

    assert result["status"] == "FULL_FILL"
    assert result["filled_quantity"] == 10_000
    assert result["strategy_id"] == AFTER_HOURS_STRATEGY_ID
    assert result["broker_called"] is False
