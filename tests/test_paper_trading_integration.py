from __future__ import annotations

import importlib.util
from pathlib import Path

import paper_trading_store
import signal_ledger
from state_store import read_json


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "paper-trading" / "scripts" / "paper_trading_runner.py"
SPEC = importlib.util.spec_from_file_location("paper_trading_integration_runner", SCRIPT)
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


def test_event_first_open_rerun_and_projection_round_trip(tmp_path, monkeypatch):
    ledger = tmp_path / "signal_ledger.jsonl"
    account_file = tmp_path / "paper_portfolio.json"
    monkeypatch.setattr(paper_trading_store, "LEDGER_FILE", str(ledger))
    monkeypatch.setattr(paper_trading_store, "ACCOUNT_FILE", str(account_file))
    monkeypatch.setattr(runner.store, "LEDGER_FILE", str(ledger))
    monkeypatch.setattr(runner.store, "ACCOUNT_FILE", str(account_file))
    surface = {
        "schema": "open_confirmation_v3",
        "asof": "2026-07-13",
        "generated_at": "2026-07-13T09:35:20+08:00",
        "status": "ready",
        "input_snapshot": {"snapshot_id": "snap", "source_versions": {"quote": "v1"}},
        "signals": [{
            "code": "600001",
            "name": "示例",
            "decision": "buy",
            "open_score": 86,
            "strategy_id": "trend:test",
            "sector": "算力",
            "quality_report": {"status": "passed"},
            "execution_controls": {"status": "estimate_only"},
            "execution_plan": {"decision": "buy", "position_pct": 10, "max_chase_price": 11, "stop_price": 9.2, "target_price": 12},
            "research_evidence": {"chanlun": {"signals": [{"type": "third_buy", "idx": 119, "date": "2026-07-13", "signal_age_bars": 0}]}},
        }],
    }
    quote = {"sh600001": {"price": 10, "prev_close": 9.8, "open": 9.9, "high": 10.1, "low": 9.8, "volume": 100_000, "fetched_at": "2026-07-13T09:36:00+08:00"}}

    first = runner.run_open(
        surface,
        quote,
        asof="2026-07-13",
        observed_at="2026-07-13T09:36:10+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )
    second = runner.run_open(
        surface,
        quote,
        asof="2026-07-13",
        observed_at="2026-07-13T09:36:10+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )

    events = signal_ledger.read_events(str(ledger))
    assert first["filled"] == 1
    assert second["reused"] == 1
    assert sum(event["event_type"] == "paper.trade.filled" for event in events) == 1
    assert read_json(str(account_file), {})["positions"][0]["code"] == "600001"
