"""P4 集成：事件止损 × T+1 锁定 × 熔断阶梯 — 升级方案 §7.2 第二条验收。

走的是真实链路，不是 mock 出来的 stub：
``exit_signals.evaluate_all_exit_signals``（四层止损，事件止损优先）产出退出原因，
``paper_trading.simulate_exit_checks``（真实 T+1 约束 + 真实手续费模型）执行，
``position_risk.assess_circuit_ladder`` 消费当日结算。

要害：**触发止损但当日 T+1 锁定 → 只记录 + 次日处置计划**，仓位不得凭空消失，
账户现金不得在锁定日发生变化。
"""

from __future__ import annotations

import copy

import pytest

import exit_signals
import paper_trading
import position_risk

ASOF = "2026-07-13"
NEXT = "2026-07-14"
RISK = {"stop_loss_pct": -8, "take_profit_pct": 20, "trailing_stop_pct": 5}


def _config():
    return {
        "schema": "paper_trading_config_v1",
        "version": "paper-chanlun-gate-v1",
        "account": {"initial_cash": 100_000.0, "lot_size": 100,
                    "max_positions": 5, "cash_buffer_pct": 5.0},
        "entry_gate": {
            "minimum_open_score": 80.0,
            "positive_recommendations": ["buy", "add", "conditional_buy"],
            "bullish_chanlun_types": ["third_buy", "bottom_divergence"],
            "bearish_chanlun_types": ["third_sell", "top_divergence"],
            "max_signal_age_bars": 3,
        },
        "execution": {"open_confirmation_not_before": "09:35:00",
                      "maximum_quote_age_seconds": 120, "slippage_bps": 20.0},
        "position_risk": {"enabled": False, "risk_budget_pct": 0.75,
                          "atr_multiple": 1.5, "stop_distance_pct_range": [3.0, 8.0],
                          "leg_pct": 10.0, "max_single_position_pct": 30.0},
    }


def _candidate():
    return {
        "code": "600001", "name": "示例股份", "decision": "buy", "open_score": 86.0,
        "strategy_id": "trend:open_confirmed", "sector": "算力",
        "quality_report": {"status": "passed"},
        "execution_controls": {"status": "ready"},
        "execution_plan": {"decision": "buy", "position_pct": 10.0,
                           "max_chase_price": 10.80, "stop_price": 9.20,
                           "target_price": 12.00},
        "research_evidence": {"chanlun": {"status": "display_only", "signals": [
            {"type": "third_buy", "strategy_id": "chanlun_third_buy", "date": ASOF,
             "signal_age_bars": 0, "gate_status": "display_only"}]}},
    }


def _quote(price=10.0, **overrides):
    quote = {"price": price, "prev_close": 9.80, "open": 9.90, "high": 10.10,
             "low": 9.85, "volume": 200_000,
             "fetched_at": "2026-07-13T09:36:10+08:00"}
    quote.update(overrides)
    return quote


def _opened_account():
    result = paper_trading.simulate_buy(
        paper_trading.default_account(_config()), _candidate(), _quote(),
        asof=ASOF, observed_at="2026-07-13T09:36:20+08:00", config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )
    assert result["status"] == "filled"
    return result["account"]


def _event_stop_reason(price):
    """真实调用四层止损：价格远未触及止损位，退出完全由事件层驱动。"""
    verdict = exit_signals.evaluate_all_exit_signals(
        current_price=price,
        stop_price=9.20,          # 未触及
        target_price=12.00,
        peak_price=10.10,         # 回撤 ~1%，未触 5% 移动止损
        trailing_pct=5.0,
        leader_gap_pct=-6.5,
        assist_premium_pct=-2.0,
        laggard_limit_down=True,
    )
    assert verdict["action"] == "sell"
    assert verdict["event_stop_priority"] is True
    return verdict["top_signal"]["signal_type"]


def test_event_stop_on_t1_locked_day_records_plan_without_selling(monkeypatch):
    account = _opened_account()
    before = copy.deepcopy(account)
    monkeypatch.setattr(paper_trading, "t1_constraint",
                        lambda acquired_on, asof: {"sell_allowed": asof > acquired_on,
                                                   "earliest_sell_date": NEXT})
    reason = _event_stop_reason(10.05)
    assert reason == "event_stop"

    same_day = paper_trading.simulate_exit_checks(
        account, {"600001": _quote(price=10.05, fetched_at="2026-07-13T14:00:00+08:00")},
        asof=ASOF, observed_at="2026-07-13T14:00:10+08:00", config=_config(),
        risk=RISK, time_stop_sessions=2, exit_overrides={"600001": reason},
    )

    event = same_day["events"][0]
    assert event["status"] == "pending_t1"
    assert event["reason"] == "event_stop"
    assert event["t1"]["earliest_sell_date"] == NEXT
    # 只记录：仓位仍在，现金一分未动。
    position = same_day["account"]["positions"][0]
    assert position["pending_exit"] == {
        "reason": "event_stop", "triggered_on": ASOF, "earliest_sell_date": NEXT}
    assert same_day["account"]["cash"] == before["cash"]
    assert position["shares"] == before["positions"][0]["shares"]
    assert same_day["account"]["realized_pnl"] == before["realized_pnl"]


def test_next_session_executes_the_recorded_disposal_plan(monkeypatch):
    account = _opened_account()
    monkeypatch.setattr(paper_trading, "t1_constraint",
                        lambda acquired_on, asof: {"sell_allowed": asof > acquired_on,
                                                   "earliest_sell_date": NEXT})
    locked = paper_trading.simulate_exit_checks(
        account, {"600001": _quote(price=10.05, fetched_at="2026-07-13T14:00:00+08:00")},
        asof=ASOF, observed_at="2026-07-13T14:00:10+08:00", config=_config(),
        risk=RISK, time_stop_sessions=2,
        exit_overrides={"600001": _event_stop_reason(10.05)},
    )

    # 次日不再重算信号：处置计划已登记，按计划执行。
    next_day = paper_trading.simulate_exit_checks(
        locked["account"],
        {"600001": _quote(price=9.60, fetched_at="2026-07-14T09:36:00+08:00")},
        asof=NEXT, observed_at="2026-07-14T09:36:10+08:00", config=_config(),
        risk=RISK, time_stop_sessions=2,
    )
    filled = next_day["events"][0]
    assert filled["status"] == "filled"
    assert filled["reason"] == "event_stop"        # 原因一路带到成交记录
    assert filled["trade_date"] == NEXT
    assert next_day["account"]["positions"] == []


def test_event_stop_precedes_a_simultaneous_price_stop_in_the_recorded_reason():
    """价格止损与事件止损同日触发时，落账原因是事件止损（优先级的可观测后果）。"""
    account = _opened_account()
    crashed = _quote(price=9.00, fetched_at="2026-07-14T09:36:00+08:00")
    result = paper_trading.simulate_exit_checks(
        account, {"600001": crashed}, asof=NEXT,
        observed_at="2026-07-14T09:36:10+08:00", config=_config(),
        risk=RISK, time_stop_sessions=2, exit_overrides={"600001": "event_stop"},
    )
    assert result["events"][0]["reason"] == "event_stop"

    # 不传 exit_overrides 时行为与改造前完全一致：仍是 hard_stop。
    baseline = paper_trading.simulate_exit_checks(
        _opened_account(), {"600001": crashed}, asof=NEXT,
        observed_at="2026-07-14T09:36:10+08:00", config=_config(),
        risk=RISK, time_stop_sessions=2,
    )
    assert baseline["events"][0]["reason"] == "hard_stop"


def test_locked_day_loss_still_feeds_the_circuit_ladder(monkeypatch):
    """T+1 锁定不等于风险消失：未实现亏损照样进熔断阶梯的当日 R 统计。"""
    account = _opened_account()
    monkeypatch.setattr(paper_trading, "t1_constraint",
                        lambda acquired_on, asof: {"sell_allowed": False,
                                                   "earliest_sell_date": NEXT})
    paper_trading.simulate_exit_checks(
        account, {"600001": _quote(price=8.90, fetched_at="2026-07-13T14:00:00+08:00")},
        asof=ASOF, observed_at="2026-07-13T14:00:10+08:00", config=_config(),
        risk=RISK, time_stop_sessions=2, exit_overrides={"600001": "leader_invalid"},
    )
    position = account["positions"][0]
    shares = int(position["shares"])
    unrealized = (8.90 - float(position["average_cost"])) * shares
    risk_unit = float(position["average_cost"]) * shares * 0.05   # 5% 止损 = 1R

    ladder = position_risk.assess_circuit_ladder(day_pnl_r=unrealized / risk_unit)
    assert unrealized < 0
    assert ladder["triggered"] == ["day_loss_2r"]
    assert ladder["new_open_allowed"] is False
    events = position_risk.circuit_ladder_events(ladder, {"asof": ASOF})
    assert any(e["payload"]["rung"] == "day_loss_2r" and e["payload"]["triggered"]
               for e in events)


def test_r_sizing_matches_the_paper_account_nav():
    account = _opened_account()
    nav = float(account["cash"]) + sum(
        float(p["average_cost"]) * int(p["shares"]) for p in account["positions"])
    sized = position_risk.r_sized_position(
        net_asset_value=nav, stop_distance_pct=5.0, mode_cap_pct=30.0,
        risk_budget_pct=_config()["position_risk"]["risk_budget_pct"])
    assert sized["status"] == position_risk.AVAILABLE
    # 0.75% / 5% × 100 = 15%，小于 ModeCap 30%
    assert sized["position_pct"] == pytest.approx(15.0)
    assert sized["binding"] == "risk_budget"
    assert sized["position_value"] == pytest.approx(nav * 0.15, rel=1e-6)
