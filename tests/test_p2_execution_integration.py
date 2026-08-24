from __future__ import annotations

import importlib.util
from pathlib import Path

import recommendation_audit
from state_store import atomic_write_json


ROOT = Path(__file__).resolve().parents[1]
OPEN_PATH = ROOT / "skills/daban-stock-picker/scripts/open_confirmation.py"
OPEN_SPEC = importlib.util.spec_from_file_location("p2_open_confirmation", OPEN_PATH)
open_confirmation = importlib.util.module_from_spec(OPEN_SPEC)
assert OPEN_SPEC and OPEN_SPEC.loader
OPEN_SPEC.loader.exec_module(open_confirmation)
BACKTEST_PATH = ROOT / "skills/chanlun-backtest/scripts/portfolio_backtest.py"
SPEC = importlib.util.spec_from_file_location("p2_portfolio_backtest", BACKTEST_PATH)
portfolio_backtest = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(portfolio_backtest)


def _recommendation_dependencies(tmp_path, monkeypatch):
    monkeypatch.setattr(
        recommendation_audit,
        "RECOMMENDATIONS_FILE",
        str(tmp_path / "recommendations.json"),
    )
    monkeypatch.setattr(recommendation_audit, "LEDGER_FILE", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setattr(recommendation_audit, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    atomic_write_json(
        recommendation_audit.PORTFOLIO_FILE,
        {"cash": 100_000, "positions": [], "cash_reconciled": True},
    )
    monkeypatch.setattr(
        recommendation_audit,
        "position_guidance",
        lambda *args, **kwargs: {
            "recommended_position_pct": 10.0,
            "recommended_amount": 10_000.0,
        },
    )
    monkeypatch.setattr(
        recommendation_audit,
        "build_quality_report",
        lambda *args, **kwargs: {"status": "passed", "execution_constraints": {}},
    )
    monkeypatch.setattr(
        recommendation_audit,
        "merge_market_intelligence",
        lambda quality, intelligence: quality,
    )
    monkeypatch.setattr(
        recommendation_audit,
        "build_research_evidence",
        lambda *args, **kwargs: {
            "market_intelligence": {"directional_ready": True},
            "chanlun": {},
        },
    )
    monkeypatch.setattr(
        recommendation_audit,
        "evaluate_candidate",
        lambda *args, **kwargs: {"status": "passed", "allowed": True},
    )
    monkeypatch.setattr(
        recommendation_audit.trading_discipline,
        "assess_discipline_state",
        lambda *args, **kwargs: {"blocked": False, "reasons": []},
    )
    monkeypatch.setattr(
        recommendation_audit,
        "evaluate_decision",
        lambda **kwargs: {
            "decision": "buy",
            "position_multiplier": 1.0,
            "guardrail": None,
            "reasons": [],
        },
    )
    monkeypatch.setattr(
        recommendation_audit.strategy_registry,
        "live_record",
        lambda strategy_id: {"strategy_id": strategy_id, "runtime_allowed": True},
    )
    monkeypatch.setattr(
        recommendation_audit,
        "read_market_context",
        lambda: {"status": "ok", "context_status": "fresh"},
    )


def _record_kwargs(**overrides):
    values = {
        "code": "600001",
        "name": "示例股份",
        "action": "buy",
        "price_range": "9.90-10.10",
        "rationale": "严格执行契约集成测试",
        "entry_price": 10.0,
        "target_price": 11.0,
        "stop_price": 9.5,
        "strategy_id": "trend_pullback",
        "sector": "测试行业",
        "announcements": [],
        "asof": "2026-07-10",
    }
    values.update(overrides)
    return values


def _known_execution_context():
    return {
        "strict_execution": True,
        "decision_mode": "live",
        "point_in_time": _pit_contract("2026-07-10", "live"),
        "listing_date": "2020-01-01",
        "listing_stage": "normal",
        "is_st": False,
        "direction": "buy",
        "directional_eligible": True,
        "limit_queue": False,
        "executable_price": 10.0,
        "available_volume": 10_000,
        "adv_value": None,
        "corporate_action_status": "unknown",
    }


def _pit_contract(day: str, mode: str = "replay"):
    return {
        "schema": "pit_stage_contract_v1",
        "decision_mode": mode,
        "event_asof": day,
        "evidence_time": f"{day}T09:34:00+08:00",
        "captured_at": f"{day}T09:35:00+08:00",
        "stage_policy": {
            "schema": "pit_stage_contract_v1",
            "stage": "open_confirmation",
            "cutoff_time": "09:35:00",
            "timezone": "Asia/Shanghai",
            "publication_delay_seconds": 0,
        },
    }


def test_recommendation_strict_unknown_tradeability_cannot_open_signal(
    tmp_path, monkeypatch
):
    _recommendation_dependencies(tmp_path, monkeypatch)
    context = _known_execution_context()
    context["listing_date"] = None

    result = recommendation_audit.record_recommendation(
        **_record_kwargs(execution_context=context)
    )

    assert result["record"]["action"] == "hold"
    assert result["record"]["settleable_signal"] is False
    assert result["record"]["execution_analysis"]["status"] == "blocked"
    assert result["record"]["execution_analysis"]["reason"] == "rule_unknown"
    assert [
        event["event_type"]
        for event in recommendation_audit.signal_ledger.read_events(
            recommendation_audit.LEDGER_FILE
        )
    ] == ["recommendation.created"]


def test_recommendation_without_strict_evidence_fails_closed(tmp_path, monkeypatch):
    _recommendation_dependencies(tmp_path, monkeypatch)
    result = recommendation_audit.record_recommendation(
        **_record_kwargs(execution_context=None)
    )
    assert result["record"]["action"] == "hold"
    assert result["record"]["settleable_signal"] is False
    assert result["record"]["execution_analysis"]["reason"] == "point_in_time_missing"


def test_recommendation_missing_transport_eligibility_fails_closed(
    tmp_path, monkeypatch
):
    _recommendation_dependencies(tmp_path, monkeypatch)
    context = _known_execution_context()
    context.pop("directional_eligible")

    result = recommendation_audit.record_recommendation(
        **_record_kwargs(execution_context=context)
    )

    assert result["record"]["action"] == "hold"
    assert result["record"]["settleable_signal"] is False
    assert result["record"]["execution_analysis"]["reason"] == (
        "transport_lower_trust"
    )


def test_recommendation_exposes_scenarios_costs_and_estimate_only_pnl(
    tmp_path, monkeypatch
):
    _recommendation_dependencies(tmp_path, monkeypatch)

    result = recommendation_audit.record_recommendation(
        **_record_kwargs(execution_context=_known_execution_context())
    )

    analysis = result["record"]["execution_analysis"]
    assert result["record"]["action"] == "buy"
    assert analysis["status"] == "estimate_only"
    assert analysis["scenarios"]["signal"]["status"] == "signal_only"
    assert analysis["scenarios"]["conditional_fill"]["status"] == "filled"
    assert analysis["scenarios"]["conservative"]["status"] == "filled"
    assert analysis["scenarios"]["capacity"]["status"] == "capacity_unknown"
    assert analysis["entry_cost_estimate"]["total"] > 0
    assert analysis["target_pnl_estimate"]["reconciliation_required"] is True


def test_open_confirmation_replay_without_pit_is_blocked(monkeypatch):
    monkeypatch.setattr(
        open_confirmation,
        "_enrich_decision",
        lambda item, announcements, asof: {
            **item,
            "decision": "buy",
            "execution_plan": {"decision": "buy", "position_pct": 4.0},
            "quality_report": {"status": "passed"},
        },
    )
    factor = {
        "code": "sh600001",
        "name": "示例股份",
        "strict_execution": True,
        "decision_mode": "replay",
        "listing_date": "2020-01-01",
        "listing_stage": "normal",
        "is_st": False,
    }
    quote = {
        "price": 10.5,
        "prev_close": 10.0,
        "open": 10.4,
        "high": 10.6,
        "low": 10.3,
        "volume": 1000,
        "change_pct": 5.0,
        "directional_eligible": True,
    }

    result = open_confirmation.evaluate_open_confirmation(
        factor, quote, asof="2026-07-10"
    )

    assert result["execution_controls"]["status"] == "blocked"
    assert result["execution_controls"]["reason"] == "point_in_time_missing"
    assert result["decision"] == "watch"
    assert result["execution_plan"]["position_pct"] == 0.0


def test_open_confirmation_live_without_strict_metadata_is_blocked(monkeypatch):
    monkeypatch.setattr(
        open_confirmation,
        "_enrich_decision",
        lambda item, announcements, asof: {
            **item,
            "decision": "buy",
            "execution_plan": {"decision": "buy", "position_pct": 4.0},
            "quality_report": {"status": "passed"},
        },
    )
    result = open_confirmation.evaluate_open_confirmation(
        {"code": "sh600001", "name": "示例股份"},
        {"price": 10.5, "prev_close": 10.0, "volume": 1000},
        asof="2026-07-10",
    )
    assert result["decision"] == "watch"
    assert result["execution_controls"]["reason"] == "point_in_time_missing"


def test_open_confirmation_strict_path_exposes_cost_and_fill_scenarios(monkeypatch):
    monkeypatch.setattr(
        open_confirmation,
        "_enrich_decision",
        lambda item, announcements, asof: {
            **item,
            "decision": "buy",
            "execution_plan": {"decision": "buy", "position_pct": 4.0},
            "quality_report": {"status": "passed"},
        },
    )
    factor = {
        "code": "sh600001",
        "name": "示例股份",
        "strict_execution": True,
        "decision_mode": "live",
        "point_in_time": _pit_contract("2026-07-10", "live"),
        "listing_date": "2020-01-01",
        "listing_stage": "normal",
        "is_st": False,
    }
    quote = {
        "price": 10.5,
        "prev_close": 10.0,
        "open": 10.4,
        "high": 10.6,
        "low": 10.3,
        "volume": 1000,
        "change_pct": 5.0,
        "directional_eligible": True,
    }

    result = open_confirmation.evaluate_open_confirmation(
        factor, quote, asof="2026-07-10"
    )

    controls = result["execution_controls"]
    assert controls["status"] == "estimate_only"
    assert controls["scenarios"]["conditional_fill"]["status"] == "filled"
    assert controls["scenarios"]["capacity"]["status"] == "capacity_unknown"
    assert controls["entry_cost_estimate"]["total"] > 0

    quote["directional_eligible"] = False
    blocked = open_confirmation.evaluate_open_confirmation(
        factor, quote, asof="2026-07-10"
    )
    assert blocked["execution_controls"]["reason"] == "transport_lower_trust"
    assert blocked["decision"] == "watch"


def _backtest_payload(strict_candidate):
    bars = [
        {"date": "2026-07-09", "open": 10, "high": 10.1, "low": 9.9, "close": 10, "volume": 100_000},
        {"date": "2026-07-10", "open": 10, "high": 10.6, "low": 9.9, "close": 10.5, "volume": 100_000},
        {"date": "2026-07-13", "open": 10.5, "high": 11.1, "low": 10.4, "close": 11, "volume": 100_000},
    ]
    return {
        "schema": "portfolio_backtest_input_v1",
        "strategy_id": "strict-v1",
        "weights": {"score": 1.0},
        "policy": {
            "initial_cash": 100_000,
            "top_n": 1,
            "max_positions": 1,
            "minimum_holding_sessions": 1,
            "commission": 0.0,
            "stamp_tax": 0.0,
            "slippage": 0.0,
            "lot_size": 100,
        },
        "snapshots": [{
            "date": "2026-07-09",
            "generated_at": "2026-07-09T09:35:00+08:00",
            "source_versions": {"quotes": "fixture-v1"},
            "candidates": [{
                "code": "600001",
                "name": "示例股份",
                "score": 90,
                "evidence_asof": "2026-07-09T09:34:00+08:00",
                "strict_execution": True,
                "decision_mode": "replay",
                **strict_candidate,
            }],
        }],
        "bars_by_code": {"600001": bars},
        "benchmark_bars": bars,
    }


def test_backtest_strict_unknown_rule_rejects_instead_of_guessing():
    payload = _backtest_payload({
        "listing_date": None,
        "listing_stage": "normal",
        "is_st": False,
        "point_in_time": _pit_contract("2026-07-09"),
    })

    result = portfolio_backtest.run_portfolio(payload)

    assert result["metrics"]["closed_trades"] == 0
    assert result["rejections"][0]["reason"] == "rule_unknown"


def test_backtest_rejects_self_asserted_or_invalid_pit_contract():
    payload = _backtest_payload({
        "listing_date": "2020-01-01",
        "listing_stage": "normal",
        "is_st": False,
        "point_in_time": {"status": "valid"},
    })

    result = portfolio_backtest.run_portfolio(payload)

    assert result["metrics"]["closed_trades"] == 0
    assert result["rejections"][0]["reason"] == "point_in_time_invalid"


def test_backtest_trade_exposes_execution_and_cost_estimates():
    payload = _backtest_payload({
        "listing_date": "2020-01-01",
        "listing_stage": "normal",
        "is_st": False,
        "point_in_time": _pit_contract("2026-07-09"),
        "corporate_action_status": "unknown",
    })

    result = portfolio_backtest.run_portfolio(payload)

    trade = result["trades"][0]
    assert trade["execution_scenarios"]["signal"]["status"] == "signal_only"
    assert trade["execution_scenarios"]["conditional_fill"]["status"] == "filled"
    assert trade["execution_scenarios"]["capacity"]["status"] == "capacity_unknown"
    assert trade["cost_estimate"]["total"] > 0
    assert trade["pnl_estimate"]["status"] == "estimate_only"
    assert trade["pnl_estimate"]["reconciliation_required"] is True
