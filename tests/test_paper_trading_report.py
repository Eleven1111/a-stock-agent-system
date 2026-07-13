from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "paper_trading_report.py"
SPEC = importlib.util.spec_from_file_location("paper_trading_report", SCRIPT)
reporter = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(reporter)


def _event(event_type, payload):
    return {"event_type": event_type, "payload": payload}


def test_report_summarizes_gate_execution_and_account_returns():
    events = [
        _event("paper.candidate_evaluated", {"asof": "2026-07-13", "gate": {"allowed": True, "reason": "recommendation_then_chanlun_passed"}}),
        _event("paper.candidate_evaluated", {"asof": "2026-07-13", "gate": {"allowed": False, "reason": "chanlun_bullish_filter_not_met"}}),
        _event("paper.trade.filled", {"trade": {"side": "buy", "code": "600001", "trade_date": "2026-07-13"}}),
        _event("paper.trade.closed", {"side": "sell", "code": "600001", "trade_date": "2026-07-14", "realized_pnl": 500}),
        _event("paper.daily_nav", {"asof": "2026-07-13", "status": "ok", "nav": 100_000}),
        _event("paper.daily_nav", {"asof": "2026-07-14", "status": "ok", "nav": 100_500}),
        _event("paper.daily_nav", {"asof": "2026-07-15", "status": "ok", "nav": 99_000}),
    ]

    report = reporter.build_report(events, initial_cash=100_000)

    assert report["status"] == "ready"
    assert report["candidate_gate"]["evaluated"] == 2
    assert report["candidate_gate"]["passed"] == 1
    assert report["candidate_gate"]["rejection_reasons"] == {"chanlun_bullish_filter_not_met": 1}
    assert report["execution"]["buys"] == 1
    assert report["execution"]["closed_trades"] == 1
    assert report["execution"]["win_rate"] == 1.0
    assert report["account"]["total_return_pct"] == -1.0
    assert report["account"]["max_drawdown_pct"] == pytest.approx(-1.4925, abs=0.0001)
    assert report["live_policy_effect"] == "none"


def test_report_fails_closed_without_nav_history():
    report = reporter.build_report([], initial_cash=100_000)
    assert report["status"] == "insufficient_data"
    assert report["account"]["final_nav"] is None
