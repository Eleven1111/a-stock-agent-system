from __future__ import annotations

from datetime import datetime

import pytest

import event_projection
import paper_trading


ASOF = "2026-07-13"


def _candidate(**overrides):
    candidate = {
        "code": "600001",
        "name": "示例股份",
        "decision": "buy",
        "open_score": 86.0,
        "strategy_id": "trend:open_confirmed",
        "sector": "算力",
        "quality_report": {"status": "passed"},
        "execution_controls": {"status": "ready"},
        "execution_plan": {
            "decision": "buy",
            "position_pct": 10.0,
            "max_chase_price": 10.80,
            "stop_price": 9.20,
            "target_price": 12.00,
        },
        "research_evidence": {
            "chanlun": {
                "status": "display_only",
                "signals": [
                    {
                        "type": "third_buy",
                        "strategy_id": "chanlun_third_buy",
                        "date": ASOF,
                        "signal_age_bars": 0,
                        "gate_status": "display_only",
                    }
                ],
            }
        },
    }
    candidate.update(overrides)
    return candidate


def _quote(price=10.0, **overrides):
    quote = {
        "price": price,
        "prev_close": 9.80,
        "open": 9.90,
        "high": 10.10,
        "low": 9.85,
        "volume": 200_000,
        "fetched_at": "2026-07-13T09:36:10+08:00",
    }
    quote.update(overrides)
    return quote


def _config():
    return {
        "schema": "paper_trading_config_v1",
        "version": "paper-chanlun-gate-v1",
        "account": {
            "initial_cash": 100_000.0,
            "lot_size": 100,
            "max_positions": 5,
            "cash_buffer_pct": 5.0,
        },
        "entry_gate": {
            "minimum_open_score": 80.0,
            "positive_recommendations": ["buy", "add", "conditional_buy"],
            "bullish_chanlun_types": ["third_buy", "bottom_divergence"],
            "bearish_chanlun_types": ["third_sell", "top_divergence"],
            "max_signal_age_bars": 3,
        },
        "execution": {
            "open_confirmation_not_before": "09:35:00",
            "maximum_quote_age_seconds": 120,
            "slippage_bps": 20.0,
        },
    }


def test_recommendation_precedes_chanlun_and_both_are_required():
    passed = paper_trading.evaluate_entry_gate(_candidate(), _config())
    assert passed["allowed"] is True
    assert passed["gate_order"] == [
        "open_recommendation",
        "open_confirmation",
        "chanlun_filter",
        "execution_checks",
    ]

    not_recommended = paper_trading.evaluate_entry_gate(
        _candidate(decision="watch"), _config()
    )
    assert not_recommended["allowed"] is False
    assert not_recommended["reason"] == "recommendation_not_positive"

    no_chanlun = _candidate(
        research_evidence={"chanlun": {"status": "no_signal", "signals": []}}
    )
    rejected = paper_trading.evaluate_entry_gate(no_chanlun, _config())
    assert rejected["allowed"] is False
    assert rejected["reason"] == "chanlun_bullish_filter_not_met"


def test_newer_bearish_chanlun_signal_vetoes_bullish_signal():
    candidate = _candidate()
    candidate["research_evidence"]["chanlun"]["signals"].append(
        {
            "type": "top_divergence",
            "strategy_id": "chanlun_top_divergence",
            "date": ASOF,
            "signal_age_bars": 0,
            "idx": 119,
            "gate_status": "display_only",
        }
    )
    candidate["research_evidence"]["chanlun"]["signals"][0]["idx"] = 118

    result = paper_trading.evaluate_entry_gate(candidate, _config())

    assert result["allowed"] is False
    assert result["reason"] == "chanlun_bearish_veto"


def test_same_bar_bearish_chanlun_signal_fails_closed():
    candidate = _candidate()
    candidate["research_evidence"]["chanlun"]["signals"][0]["idx"] = 119
    candidate["research_evidence"]["chanlun"]["signals"].append(
        {
            "type": "third_sell",
            "strategy_id": "chanlun_third_sell",
            "date": ASOF,
            "signal_age_bars": 0,
            "idx": 119,
        }
    )
    assert paper_trading.evaluate_entry_gate(candidate, _config())["reason"] == (
        "chanlun_bearish_veto"
    )


def test_gate_rejects_stale_or_low_score_evidence():
    stale = _candidate()
    stale["research_evidence"]["chanlun"]["signals"][0]["signal_age_bars"] = 4
    assert paper_trading.evaluate_entry_gate(stale, _config())["reason"] == (
        "chanlun_bullish_filter_not_met"
    )
    assert paper_trading.evaluate_entry_gate(
        _candidate(open_score=79.9), _config()
    )["reason"] == "recommendation_score_below_threshold"


def test_open_surface_must_be_same_day_and_after_confirmation_time():
    payload = {
        "schema": "open_confirmation_v3",
        "asof": ASOF,
        "generated_at": "2026-07-13T09:35:30+08:00",
        "status": "ready",
        "input_snapshot": {"snapshot_id": "snap-1", "source_versions": {"quote": "v1"}},
        "signals": [_candidate()],
    }
    validated = paper_trading.validate_open_surface(
        payload,
        asof=ASOF,
        observed_at="2026-07-13T09:36:00+08:00",
        config=_config(),
    )
    assert validated["status"] == "ready"

    with pytest.raises(ValueError, match="before_open_confirmation"):
        paper_trading.validate_open_surface(
            payload,
            asof=ASOF,
            observed_at="2026-07-13T09:34:59+08:00",
            config=_config(),
        )
    with pytest.raises(ValueError, match="trading_date_mismatch"):
        paper_trading.validate_open_surface(
            payload,
            asof="2026-07-14",
            observed_at="2026-07-14T09:36:00+08:00",
            config=_config(),
        )


def test_buy_uses_post_confirmation_quote_lots_costs_and_cash_limit():
    account = paper_trading.default_account(_config())
    result = paper_trading.simulate_buy(
        account,
        _candidate(),
        _quote(),
        asof=ASOF,
        observed_at="2026-07-13T09:36:20+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )

    assert result["status"] == "filled"
    assert result["trade"]["shares"] == 900
    assert result["trade"]["fill_price"] == 10.02
    assert result["account"]["cash"] >= 0
    assert result["account"]["positions"][0]["buy_date"] == ASOF
    assert result["account"]["positions"][0]["entry_evidence"]["chanlun"]["type"] == "third_buy"


def test_buy_fails_closed_for_stale_quote_or_limit_queue():
    account = paper_trading.default_account(_config())
    stale = paper_trading.simulate_buy(
        account,
        _candidate(),
        _quote(fetched_at="2026-07-13T09:30:00+08:00"),
        asof=ASOF,
        observed_at="2026-07-13T09:36:20+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )
    assert stale["status"] == "rejected"
    assert stale["reason"] == "quote_stale"

    limit_up = paper_trading.simulate_buy(
        account,
        _candidate(),
        _quote(price=10.78, prev_close=9.80, open=10.78, low=10.78, high=10.78),
        asof=ASOF,
        observed_at="2026-07-13T09:36:20+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )
    assert limit_up["status"] == "rejected"
    assert limit_up["reason"] == "limit_queue_unobservable"


def test_exit_trigger_respects_t1_then_fills_next_session(monkeypatch):
    account = paper_trading.simulate_buy(
        paper_trading.default_account(_config()),
        _candidate(),
        _quote(),
        asof=ASOF,
        observed_at="2026-07-13T09:36:20+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )["account"]
    falling = _quote(price=9.10, fetched_at="2026-07-13T14:00:00+08:00")

    monkeypatch.setattr(
        paper_trading,
        "t1_constraint",
        lambda acquired_on, asof: {
            "sell_allowed": asof > acquired_on,
            "earliest_sell_date": "2026-07-14",
        },
    )
    same_day = paper_trading.simulate_exit_checks(
        account,
        {"600001": falling},
        asof=ASOF,
        observed_at="2026-07-13T14:00:10+08:00",
        config=_config(),
        risk={"stop_loss_pct": -8, "take_profit_pct": 20, "trailing_stop_pct": 5},
        time_stop_sessions=2,
    )
    assert same_day["events"][0]["status"] == "pending_t1"
    assert same_day["account"]["positions"][0]["pending_exit"]["reason"] == "hard_stop"

    next_day_quote = _quote(price=9.00, fetched_at="2026-07-14T09:36:00+08:00")
    next_day = paper_trading.simulate_exit_checks(
        same_day["account"],
        {"600001": next_day_quote},
        asof="2026-07-14",
        observed_at="2026-07-14T09:36:10+08:00",
        config=_config(),
        risk={"stop_loss_pct": -8, "take_profit_pct": 20, "trailing_stop_pct": 5},
        time_stop_sessions=2,
    )
    assert next_day["events"][0]["status"] == "filled"
    assert next_day["account"]["positions"] == []
    assert next_day["account"]["cash"] < 100_000


def test_paper_account_snapshot_cannot_pollute_live_portfolio_projection():
    event = {
        "sequence": 7,
        "event_type": "paper.trade.filled",
        "payload": {"paper_account_after": {"cash": 90_000, "positions": []}},
    }
    assert event_projection.latest_portfolio_snapshot([event]) is None


def test_daily_nav_marks_positions_without_fabricating_missing_quotes():
    account = paper_trading.default_account(_config())
    account["positions"] = [{
        "code": "600001",
        "name": "示例股份",
        "shares": 100,
        "average_cost": 10.0,
        "buy_date": ASOF,
        "peak_price": 10.0,
        "sector": "算力",
    }]
    result = paper_trading.mark_to_market(
        account,
        {},
        asof=ASOF,
        observed_at=datetime(2026, 7, 13, 15, 1).isoformat(),
    )
    assert result["status"] == "blocked"
    assert result["missing_quotes"] == ["600001"]
    assert result["nav"] is None


def test_zero_fill_reason_separates_designed_gates_from_data_anomaly():
    """空仓必须能分辨「上游门禁按设计拒绝」和「数据面缺了」。

    #174：虚拟盘自 2026-07-13 建账起 0 成交，三个作业全 status=ok，输出里只有
    filled=0 / rejected=5，看不出是 fail-closed 正常还是采集异常——排查耗时
    主要就耗在这里。
    """
    gated = paper_trading.classify_zero_fill([
        {"reason": "recommendation_not_positive"},
        {"reason": "chanlun_bullish_filter_not_met"},
    ])
    assert gated["zero_fill_class"] == "upstream_gate"
    assert gated["actionable"] is False
    assert gated["breakdown"] == {
        "chanlun_bullish_filter_not_met": 1,
        "recommendation_not_positive": 1,
    }


def test_data_anomaly_outranks_designed_gates_in_the_same_batch():
    """一条数据面异常就足以让整批需要人看 —— 不能被多数正常拒绝淹没。"""
    mixed = paper_trading.classify_zero_fill([
        {"reason": "recommendation_not_positive"},
        {"reason": "recommendation_not_positive"},
        {"reason": "quote_unavailable"},
    ])
    assert mixed["zero_fill_class"] == "data_anomaly"
    assert mixed["actionable"] is True
    assert mixed["anomaly_reasons"] == ["quote_unavailable"]


def test_market_and_account_states_are_not_flagged_as_anomaly():
    """涨停买不进、仓位已满、现金不够一手都是正常状态，不该叫人来查。"""
    result = paper_trading.classify_zero_fill([
        {"reason": "limit_queue_unobservable"},
        {"reason": "max_positions_reached"},
        {"reason": "insufficient_cash_for_round_lot"},
    ])
    assert result["zero_fill_class"] == "market_or_account"
    assert result["actionable"] is False


def test_empty_candidate_surface_is_its_own_class():
    result = paper_trading.classify_zero_fill([])

    assert result["zero_fill_class"] == "no_candidates"
    assert result["actionable"] is True


def test_unknown_reason_fails_towards_review_not_silence():
    """新增的 reason 没登记时按需要人看处理，绝不静默归入正常。"""
    result = paper_trading.classify_zero_fill([{"reason": "some_new_reason_v2"}])

    assert result["zero_fill_class"] == "data_anomaly"
    assert result["actionable"] is True
    assert result["anomaly_reasons"] == ["some_new_reason_v2"]


def test_filled_batch_reports_no_zero_fill_class():
    result = paper_trading.classify_zero_fill(
        [{"reason": "recommendation_not_positive"}], filled=1,
    )

    assert result["zero_fill_class"] is None
    assert result["actionable"] is False
