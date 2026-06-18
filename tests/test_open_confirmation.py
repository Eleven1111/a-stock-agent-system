"""09:35 open confirmation pure decision tests."""

import importlib.util
from pathlib import Path

import candidate_lifecycle
from state_store import atomic_write_json, read_json

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "daban-stock-picker" / "scripts" / "open_confirmation.py"
SPEC = importlib.util.spec_from_file_location("open_confirmation", SCRIPT)
oc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(oc)


def test_open_confirmation_marks_yiziban_not_buyable():
    factor = {
        "code": "sz002156",
        "name": "通富微电",
        "auction_gap_pct": 10.0,
        "board_status": "yizi_seal",
        "is_yiziban": True,
    }
    quote = {"price": 11.0, "prev_close": 10.0, "open": 11.0, "low": 11.0, "high": 11.0, "volume": 1000}

    result = oc.evaluate_open_confirmation(factor, quote)

    assert result["action"] == "not_buyable"
    assert result["tradeability"]["tradeable"] is False


def test_open_confirmation_marks_mid_gain_as_trend_watch():
    factor = {
        "code": "sz002156",
        "name": "通富微电",
        "auction_gap_pct": 4.0,
        "board_status": "high_open",
        "is_yiziban": False,
    }
    quote = {
        "price": 10.5,
        "prev_close": 10.0,
        "open": 10.4,
        "low": 10.3,
        "high": 10.6,
        "volume": 1000,
        "change_pct": 5.0,
    }

    result = oc.evaluate_open_confirmation(factor, quote)

    assert result["action"] == "trend_watch"
    assert "3%-10%" in result["reasons"][0]
    assert result["decision"] in {"buy", "watch"}
    assert result["execution_plan"]["same_day_sell_allowed"] is False
    assert result["execution_plan"]["entry_range"]
    assert result["quality_report"]["status"] in {"passed", "conditional"}


def test_open_confirmation_policy_includes_research_evidence(monkeypatch):
    monkeypatch.setattr(
        oc,
        "build_research_evidence",
        lambda code, strategy_id, asof: {
            "schema": "research_evidence_v1",
            "chanlun": {"status": "live_allowed"},
            "serenity": {"available": True, "stale": False, "hard_risks": []},
        },
    )
    item = {
        "code": "sh600001",
        "open_selected_by": {"daban": True, "trend": False},
        "execution_plan": {"decision": "buy", "position_pct": 4.0},
        "quality_report": {"status": "passed"},
    }

    result = oc._apply_policy(item, asof="2026-06-12")

    assert result["research_evidence"]["chanlun"]["status"] == "live_allowed"


def test_rank_confirmations_returns_top_five_and_keeps_strategy_scores():
    shortlist = [
        {
            "code": f"sh60{i:04d}",
            "name": f"股票{i}",
            "auction_score": 95 - i,
            "daban_score": 90 - i,
            "trend_score": 70 + i,
        }
        for i in range(8)
    ]
    confirmations = [
        {
            "code": item["code"],
            "name": item["name"],
            "action": "watch",
            "change_pct": 5.0,
            "tradeability": {"tradeable": True, "status": "normal"},
            "reasons": [],
        }
        for item in shortlist
    ]

    ranked = oc.rank_confirmations(shortlist, confirmations, limit=5)

    assert len(ranked) == 5
    assert ranked[0]["open_rank"] == 1
    assert "daban_score" in ranked[0]
    assert "trend_score" in ranked[0]


def test_rank_confirmations_preserves_strategy_lanes():
    shortlist = [
        {
            "code": f"sh600{i:03d}",
            "name": f"打板{i}",
            "auction_score": 95 - i,
            "auction_daban_score": 95 - i,
            "auction_trend_score": 20,
            "auction_selected_by": {"daban": True, "trend": False},
        }
        for i in range(5)
    ] + [
        {
            "code": f"sz300{i:03d}",
            "name": f"趋势{i}",
            "auction_score": 90 - i,
            "auction_daban_score": 0,
            "auction_trend_score": 90 - i,
            "auction_selected_by": {"daban": False, "trend": True},
        }
        for i in range(5)
    ]
    confirmations = [
        {
            "code": item["code"],
            "name": item["name"],
            "action": "trend_watch",
            "change_pct": 5.0,
            "tradeability": {"tradeable": True, "status": "normal"},
            "reasons": [],
        }
        for item in shortlist
    ]

    ranked = oc.rank_confirmations(shortlist, confirmations, limit=5)

    assert sum(item["open_selected_by"]["daban"] for item in ranked) >= 3
    assert sum(item["open_selected_by"]["trend"] for item in ranked) >= 2


def test_rank_confirmations_enforces_temperature_daban_gate():
    shortlist = [
        {
            "code": "sh600001",
            "name": "打板候选",
            "auction_score": 99,
            "auction_daban_score": 99,
            "auction_trend_score": 10,
            "auction_selected_by": {"daban": True, "trend": False},
        },
        {
            "code": "sz300001",
            "name": "趋势候选",
            "auction_score": 80,
            "auction_daban_score": 0,
            "auction_trend_score": 80,
            "auction_selected_by": {"daban": False, "trend": True},
        },
    ]
    confirmations = [
        {
            "code": item["code"],
            "name": item["name"],
            "action": "trend_watch",
            "change_pct": 5.0,
            "tradeability": {"tradeable": True, "status": "normal"},
            "reasons": [],
        }
        for item in shortlist
    ]

    ranked = oc.rank_confirmations(
        shortlist,
        confirmations,
        limit=2,
        temperature={
            "tier": "极热",
            "allow_new_daban": False,
            "top_n_limit": 0,
            "context_fresh": True,
        },
    )

    assert [item["code"] for item in ranked] == ["sz300001"]
    assert ranked[0]["open_selected_by"]["trend"] is True


def test_build_confirmation_applies_live_retreat_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(oc.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(oc.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(oc.recommendation_audit, "RECOMMENDATIONS_FILE", str(tmp_path / "recommendations.json"))
    monkeypatch.setattr(oc.recommendation_audit, "HISTORY_FILE", str(tmp_path / "trade_history.json"))
    monkeypatch.setattr(oc.recommendation_audit, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(oc.recommendation_audit, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(oc, "scan_many", lambda codes: {str(code)[-6:]: [] for code in codes})
    source_asof = "2026-06-10"
    event_asof = "2026-06-11"
    shortlist = [
        {
            "code": "sh600001",
            "name": "昨日高度板",
            "auction_score": 99,
            "auction_daban_score": 99,
            "auction_trend_score": 10,
            "auction_selected_by": {"daban": True, "trend": False},
            "auction_gap_pct": 2.0,
            "board_status": "high_open",
            "is_yiziban": False,
        },
        {
            "code": "sz300001",
            "name": "趋势候选",
            "auction_score": 80,
            "auction_daban_score": 0,
            "auction_trend_score": 80,
            "auction_selected_by": {"daban": False, "trend": True},
            "auction_gap_pct": 2.0,
            "board_status": "high_open",
            "is_yiziban": False,
        },
    ]
    atomic_write_json(
        oc._shortlist_path(event_asof),
        {
            "schema": "auction_finalize_v2",
            "asof": event_asof,
            "source_asof": source_asof,
            "shortlist": shortlist,
        },
    )
    monkeypatch.setattr(
        oc,
        "read_signal_context",
        lambda **_kwargs: {
            "ladder_asof": source_asof,
            "lianban_ladder": {
                "600001": {"lianban": 5},
                "000002": {"lianban": 2},
            },
        },
    )

    requested = []

    def _quotes(codes):
        requested.extend(codes)
        return {
            code: {
                "name": code,
                "price": 10.5,
                "prev_close": 10.0,
                "open": 9.3 if code == "sh600001" else 10.4,
                "high": 10.6,
                "low": 9.2,
                "volume": 1_000,
                "change_pct": 5.0,
            }
            for code in codes
        }

    monkeypatch.setattr(oc, "fetch_tencent_snapshot", _quotes)

    result = oc.build_confirmation([], event_asof, limit=2)

    assert "sh600001" in requested
    assert result["market_temperature"]["retreat_signal"]
    assert result["market_temperature"]["allow_new_daban"] is False
    assert [item["code"] for item in result["signals"]] == ["sz300001"]


def test_build_confirmation_persists_top_signals_and_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(oc.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(oc.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(oc.recommendation_audit, "RECOMMENDATIONS_FILE", str(tmp_path / "recommendations.json"))
    monkeypatch.setattr(oc.recommendation_audit, "HISTORY_FILE", str(tmp_path / "trade_history.json"))
    monkeypatch.setattr(oc.recommendation_audit, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(oc.recommendation_audit, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(oc, "scan_many", lambda codes: {str(code)[-6:]: [] for code in codes})
    source_asof = "2026-06-10"
    event_asof = "2026-06-11"
    shortlist = [
        {
            "code": f"sh600{i:03d}",
            "name": f"股票{i}",
            "auction_score": 90 - i,
            "daban_score": 85 - i,
            "trend_score": 70 + i,
            "auction_gap_pct": 2.0,
            "board_status": "high_open",
            "is_yiziban": False,
        }
        for i in range(6)
    ]
    lifecycle_candidates = [
        {
            **item,
            "code": item["code"][2:],
            "selected_by": {"daban": True, "trend": False},
        }
        for item in shortlist
    ]
    candidate_lifecycle.initialize_day(source_asof, lifecycle_candidates)
    atomic_write_json(
        oc._shortlist_path(event_asof),
        {
            "schema": "auction_finalize_v2",
            "asof": event_asof,
            "source_asof": source_asof,
            "shortlist": shortlist,
        },
    )
    monkeypatch.setattr(
        oc,
        "fetch_tencent_snapshot",
        lambda codes: {
            code: {
                "name": code,
                "price": 10.5,
                "prev_close": 10.0,
                "open": 10.4,
                "high": 10.6,
                "low": 10.3,
                "volume": 1_000,
                "change_pct": 5.0,
            }
            for code in codes
        },
    )
    monkeypatch.setattr(
        oc,
        "build_research_evidence",
        lambda code, strategy_id, asof: {
            "schema": "research_evidence_v1",
            "chanlun": {
                "status": "no_signal",
                "live_bullish_signals": [],
                "live_bearish_signals": [],
            },
            "serenity": {
                "available": False,
                "stale": None,
                "hard_risks": [],
            },
            "market_intelligence": {
                "available": True,
                "stale": False,
                "directional_ready": True,
                "hard_risks": [],
                "warnings": [],
            },
        },
    )

    result = oc.build_confirmation([], event_asof, limit=3)

    assert result["signal_count"] == 3
    assert read_json(oc._confirmation_path(event_asof), {})["signal_count"] == 3
    assert all(item["ledger_links"]["correlation_id"] for item in result["signals"])
    events = oc.signal_ledger.read_events(oc.recommendation_audit.LEDGER_FILE)
    assert sum(event["event_type"] == "signal.opened" for event in events) == 3
    assert sum(event["event_type"] == "monitor.activated" for event in events) == 3
    monitors = oc.monitor_registry.active_entries("stock", asof=event_asof)
    assert len(monitors) == 3
    assert {item["source_group"] for item in monitors} == {"open_confirmation"}
    signal = oc.signal_ledger.project_signals(events)[0]
    assert signal["recommendation_id"].startswith(f"open-{event_asof}-")
    assert signal["monitor_id"].startswith("stock:")
    lifecycle = candidate_lifecycle.load_day(source_asof)
    assert sum(record["current_stage"] == "open_confirmed" for record in lifecycle["records"]) == 3
