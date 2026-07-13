from __future__ import annotations

import importlib.util
from pathlib import Path

import paper_trading


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "paper-trading" / "scripts" / "paper_trading_runner.py"
SPEC = importlib.util.spec_from_file_location("paper_trading_runner", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)


def _config():
    return {
        "schema": "paper_trading_config_v1",
        "version": "paper-chanlun-gate-v1",
        "account": {"initial_cash": 100_000.0, "lot_size": 100, "max_positions": 5, "cash_buffer_pct": 5.0},
        "entry_gate": {
            "minimum_open_score": 80.0,
            "positive_recommendations": ["buy", "add", "conditional_buy"],
            "bullish_chanlun_types": ["third_buy", "bottom_divergence"],
            "bearish_chanlun_types": ["third_sell", "top_divergence"],
            "max_signal_age_bars": 3,
        },
        "execution": {"open_confirmation_not_before": "09:35:00", "maximum_quote_age_seconds": 120, "slippage_bps": 20.0},
    }


def _candidate(code="600001", decision="buy"):
    return {
        "code": code,
        "name": "示例",
        "decision": decision,
        "open_score": 85,
        "strategy_id": "trend:test",
        "sector": "算力",
        "quality_report": {"status": "passed"},
        "execution_controls": {"status": "estimate_only"},
        "execution_plan": {"decision": decision, "position_pct": 10, "max_chase_price": 11, "stop_price": 9, "target_price": 12},
        "research_evidence": {"chanlun": {"signals": [{"type": "third_buy", "idx": 119, "date": "2026-07-13", "signal_age_bars": 0}]}},
    }


def _surface():
    return {
        "schema": "open_confirmation_v3",
        "asof": "2026-07-13",
        "generated_at": "2026-07-13T09:35:20+08:00",
        "status": "ready",
        "input_snapshot": {"snapshot_id": "snap", "source_versions": {"quote": "v1"}},
        "signals": [_candidate(), _candidate("600002", decision="watch")],
    }


def test_open_run_evaluates_every_recommendation_but_only_buys_passed_gate(monkeypatch):
    events = []
    account = paper_trading.default_account(_config())
    monkeypatch.setattr(runner.store, "load_account", lambda config: account)
    monkeypatch.setattr(runner.store, "event_exists", lambda *args: False)

    def append(event_type, *, payload, idempotency_key, config, account_after=None, links=None):
        events.append((event_type, payload, account_after))
        return {"status": "appended"}

    monkeypatch.setattr(runner.store, "append_paper_event", append)
    quotes = {
        "sh600001": {"price": 10, "prev_close": 9.8, "open": 9.9, "high": 10.1, "low": 9.8, "volume": 100_000, "fetched_at": "2026-07-13T09:36:00+08:00"}
    }
    result = runner.run_open(
        _surface(),
        quotes,
        asof="2026-07-13",
        observed_at="2026-07-13T09:36:10+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )

    assert result["filled"] == 1
    assert result["rejected"] == 1
    assert any(kind == "paper.account.opened" for kind, _, _ in events)
    assert [kind for kind, _, _ in events].count("paper.candidate_evaluated") == 2
    assert any(kind == "paper.trade.filled" for kind, _, _ in events)
    assert all(payload.get("live_order_sent") is not True for _, payload, _ in events)


def test_open_run_is_idempotent_after_trade_was_recorded(monkeypatch):
    account = paper_trading.default_account(_config())
    monkeypatch.setattr(runner.store, "load_account", lambda config: account)
    monkeypatch.setattr(
        runner.store,
        "event_exists",
        lambda event_type, key: event_type == "paper.trade.filled" and key.endswith("600001:buy"),
    )
    recorded = []
    monkeypatch.setattr(runner.store, "append_paper_event", lambda event_type, **kwargs: recorded.append(event_type) or {"status": "appended"})

    result = runner.run_open(
        _surface(),
        {},
        asof="2026-07-13",
        observed_at="2026-07-13T09:36:10+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )
    assert result["reused"] == 1
    assert "paper.trade.filled" not in recorded


def test_paper_account_circuit_breaker_blocks_new_entries(monkeypatch):
    account = paper_trading.default_account(_config())
    monkeypatch.setattr(runner.store, "load_account", lambda config: account)
    monkeypatch.setattr(runner.store, "event_exists", lambda *args: False)
    recorded = []
    monkeypatch.setattr(runner.store, "append_paper_event", lambda event_type, **kwargs: recorded.append((event_type, kwargs["payload"])) or {"status": "appended"})

    result = runner.run_open(
        _surface(),
        {},
        asof="2026-07-13",
        observed_at="2026-07-13T09:36:10+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
        discipline_state={"blocked": True, "reasons": ["week_trade_cap"]},
    )

    assert result["filled"] == 0
    assert result["discipline_state"]["blocked"] is True
    assert any(
        kind == "paper.order.rejected" and payload["reason"] == "paper_discipline_blocked"
        for kind, payload in recorded
    )


def test_monitor_persists_pending_t1_state(monkeypatch):
    account = paper_trading.default_account(_config())
    account["positions"] = [{
        "code": "600001",
        "name": "示例",
        "shares": 100,
        "average_cost": 10.0,
        "cost": 10.0,
        "buy_date": "2026-07-13",
        "peak_price": 10.0,
        "sector": "算力",
        "lane": "trend",
        "stop_price": 9.2,
        "target_price": 12.0,
    }]
    monkeypatch.setattr(runner.store, "load_account", lambda config: account)
    monkeypatch.setattr(
        paper_trading,
        "t1_constraint",
        lambda acquired_on, asof: {"sell_allowed": False, "earliest_sell_date": "2026-07-14"},
    )
    recorded = []
    monkeypatch.setattr(
        runner.store,
        "append_paper_event",
        lambda event_type, **kwargs: recorded.append((event_type, kwargs.get("account_after"))) or {"status": "appended"},
    )
    quote = {"600001": {"price": 9.1, "prev_close": 9.8, "open": 9.5, "high": 9.5, "low": 9.1, "volume": 100_000, "fetched_at": "2026-07-13T14:00:00+08:00"}}

    runner.run_monitor(
        quote,
        asof="2026-07-13",
        observed_at="2026-07-13T14:00:10+08:00",
        config=_config(),
        risk={"stop_loss_pct": -8, "take_profit_pct": 20, "trailing_stop_pct": 5},
        time_stop_sessions=2,
    )

    event_type, snapshot = recorded[0]
    assert event_type == "paper.exit.pending_t1"
    assert snapshot["positions"][0]["pending_exit"]["reason"] == "hard_stop"
