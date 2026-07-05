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

    result = oc._apply_policy(
        item,
        asof="2026-06-12",
        portfolio={"cash": 20000, "positions": []},
    )

    assert result["research_evidence"]["chanlun"]["status"] == "live_allowed"


def test_hot_money_policy_uses_research_strategy_not_reseal_proxy(monkeypatch):
    monkeypatch.setattr(oc.strategy_registry, "live_record", lambda _strategy_id: None)
    monkeypatch.setattr(
        oc,
        "build_research_evidence",
        lambda code, strategy_id, asof: {
            "schema": "research_evidence_v1",
            "chanlun": {"live_bullish_signals": [], "live_bearish_signals": []},
            "serenity": {"available": False, "stale": None, "hard_risks": []},
            "market_intelligence": {
                "available": True,
                "stale": False,
                "directional_ready": True,
                "hard_risks": [],
                "warnings": [],
            },
        },
    )
    item = {
        "code": "sh600001",
        "sector": "半导体",
        "hot_money_qualified": True,
        "open_selected_by": {"daban": True, "trend": False},
        "execution_plan": {"decision": "buy", "position_pct": 4.0},
        "quality_report": {"status": "passed"},
        "selection_context": {"window": "09:25"},
    }

    result = oc._apply_policy(
        item,
        asof="2026-06-12",
        portfolio={"cash": 20000, "positions": []},
    )

    assert result["strategy_id"] == "daban:mainline_leader_confirm"
    assert result["decision"] == "watch"
    assert result["selection_context"]["window"] == "09:35"


def test_open_daban_lane_rejects_candidate_outside_mainline_leader_gate():
    shortlist = [
        {
            "code": "sh600001",
            "name": "非主线高分",
            "auction_score": 99,
            "auction_daban_score": 99,
            "auction_trend_score": 10,
            "hot_money_qualified": False,
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

    ranked = oc.rank_confirmations(shortlist, confirmations, limit=2)

    assert [item["code"] for item in ranked] == ["sz300001"]


def test_open_confirmation_reads_nested_selection_qualified_gate():
    shortlist = [
        {
            "code": "sh600001",
            "name": "嵌套不合格",
            "auction_score": 99,
            "auction_daban_score": 99,
            "auction_trend_score": 10,
            "auction_selected_by": {"daban": True, "trend": False},
            "selection_context": {"qualified": False},
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

    ranked = oc.rank_confirmations(shortlist, confirmations, limit=2)

    assert [item["code"] for item in ranked] == ["sz300001"]


def test_open_confirmation_blocks_weak_market_broad_sector_watch_delivery():
    shortlist = [
        {
            "code": "sh600001",
            "name": "弱市宽行业",
            "sector": "C 制造业",
            "auction_score": 92,
            "auction_daban_score": 20,
            "auction_trend_score": 92,
            "auction_selected_by": {"daban": False, "trend": True},
            "selection_context": {
                "window": "09:25",
                "market_timing": {
                    "status": "insufficient_data",
                    "breadth": {
                        "advancers": 756,
                        "decliners": 4394,
                        "flat": 55,
                        "limitup_count": 77,
                        "limitdown_count": 54,
                    },
                    "temperature": {"tier": "neutral", "context_fresh": False},
                },
                "sector": {"name": "C 制造业", "rank": 1},
                "leader": {"rank": 111},
            },
        },
    ]
    confirmations = [
        {
            "code": "sh600001",
            "name": "弱市宽行业",
            "action": "trend_watch",
            "change_pct": 4.0,
            "tradeability": {"tradeable": True, "status": "normal"},
            "reasons": [],
        }
    ]

    ranked = oc.rank_confirmations(shortlist, confirmations, limit=1)

    assert ranked == []


def test_research_only_watch_keeps_requested_buy_for_ledger_audit():
    research_only = {
        "decision": "watch",
        "policy_decision": {
            "requested_action": "buy",
            "reasons": ["strategy_unverified"],
        },
    }
    risk_blocked = {
        "decision": "watch",
        "policy_decision": {
            "requested_action": "buy",
            "reasons": ["market_risk_off"],
        },
    }

    assert oc._recommendation_action(research_only) == "buy"
    assert oc._recommendation_action(risk_blocked) == "hold"


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
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
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
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(oc.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(oc.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(oc.recommendation_audit, "RECOMMENDATIONS_FILE", str(tmp_path / "recommendations.json"))
    monkeypatch.setattr(oc.recommendation_audit, "HISTORY_FILE", str(tmp_path / "trade_history.json"))
    monkeypatch.setattr(oc.recommendation_audit, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(oc.recommendation_audit, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(
        str(tmp_path / "skills" / "stock-triage" / "data" / "portfolio.json"),
        {"cash": 20000, "positions": [], "cash_reconciled": True},
    )
    monkeypatch.setattr(
        oc.strategy_registry,
        "live_record",
        lambda strategy_id: {
            "strategy_id": strategy_id,
            "allowed_in_live_agent": True,
            "gating_status": "enabled",
            "runtime_allowed": True,
        },
    )
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
    recommendation_events = [
        event for event in events
        if event["event_type"] == "recommendation.created"
    ]
    assert len(recommendation_events) == 3
    evidence_sources = recommendation_events[0]["payload"]["evidence_sources"]
    assert evidence_sources[0]["source"] == "open-confirmation"
    assert evidence_sources[0]["weight_hint"] == "primary"
    assert evidence_sources[0]["artifact"]["snapshot_id"].startswith("snap-")
    assert evidence_sources[0]["artifact"]["consumed_from_snapshot"] is True
    assert {
        (item["source"], item["weight_hint"])
        for item in evidence_sources
    } >= {
        ("auction-finalize", "supporting"),
    }
    assert oc.signal_ledger.project_signals(events)[0]["evidence_sources"] == evidence_sources
    assert sum(event["event_type"] == "monitor.activated" for event in events) == 3
    monitors = oc.monitor_registry.active_entries("stock", asof=event_asof)
    assert len(monitors) == 3
    assert {item["source_group"] for item in monitors} == {"open_confirmation"}
    signal = oc.signal_ledger.project_signals(events)[0]
    assert signal["recommendation_id"].startswith(f"open-{event_asof}-")
    assert signal["monitor_id"].startswith("stock:")
    report = oc.json_report(result)
    assert report["signal_count"] == 3
    assert "confirmations" not in report
    assert "selection_context" in report["signals"][0]
    lifecycle = candidate_lifecycle.load_day(source_asof)
    assert sum(record["current_stage"] == "open_confirmed" for record in lifecycle["records"]) == 3


def test_apply_policy_blocks_daban_lane_when_discipline_gate_trips(monkeypatch):
    """09:35 是最终写 recommendations.json 的关口；日周止损/连续错单必须在这里也生效，
    不能只在09:26竞价收口生效，否则一个已被熔断的账户仍会在开盘确认阶段拿到buy。"""
    monkeypatch.setattr(
        oc.strategy_registry,
        "live_record",
        lambda strategy_id: {
            "strategy_id": strategy_id,
            "allowed_in_live_agent": True,
            "gating_status": "enabled",
            "runtime_allowed": True,
        },
    )
    monkeypatch.setattr(
        oc,
        "build_research_evidence",
        lambda code, strategy_id, asof: {
            "schema": "research_evidence_v1",
            "chanlun": {"status": "no_signal", "live_bullish_signals": [], "live_bearish_signals": []},
            "serenity": {"available": False, "stale": None, "hard_risks": []},
            "market_intelligence": {
                "available": True, "stale": False, "directional_ready": True,
                "hard_risks": [], "warnings": [],
            },
        },
    )
    item = {
        "code": "sh600001",
        "hot_money_qualified": True,
        "open_selected_by": {"daban": True, "trend": False},
        "execution_plan": {"decision": "buy", "position_pct": 4.0},
        "quality_report": {"status": "passed"},
    }
    kwargs = dict(asof="2026-06-12", portfolio={"cash": 20000, "positions": []})

    clean = oc._apply_policy(item, discipline_state={"blocked": False, "reasons": []}, **kwargs)
    blocked = oc._apply_policy(
        item,
        discipline_state={"blocked": True, "reasons": ["consecutive_losses_freeze"]},
        **kwargs,
    )

    assert clean["policy_decision"]["decision"] == "buy"
    assert blocked["decision"] == "avoid"
    assert blocked["policy_decision"]["decision"] == "avoid"
    assert "consecutive_losses_freeze" in blocked["policy_decision"]["reasons"]


def test_build_confirmation_computes_real_discipline_state_from_ledger(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(oc.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(oc.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(oc.recommendation_audit, "RECOMMENDATIONS_FILE", str(tmp_path / "recommendations.json"))
    monkeypatch.setattr(oc.recommendation_audit, "HISTORY_FILE", str(tmp_path / "trade_history.json"))
    monkeypatch.setattr(oc.recommendation_audit, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(oc.recommendation_audit, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(oc.signal_ledger, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(
        str(tmp_path / "skills" / "stock-triage" / "data" / "portfolio.json"),
        {"cash": 100000, "positions": [], "cash_reconciled": True},
    )
    for trade_date in ["2026-06-08", "2026-06-09", "2026-06-10"]:
        oc.signal_ledger.append_event(
            "trade.executed",
            oc.signal_ledger.make_links(f"loss-{trade_date}"),
            {"code": "600099", "action": "close", "trade_date": trade_date, "pnl": -100, "pnl_pct": -3.0},
        )
    monkeypatch.setattr(oc, "scan_many", lambda codes: {str(code)[-6:]: [] for code in codes})
    event_asof = "2026-06-11"
    atomic_write_json(
        oc._shortlist_path(event_asof),
        {"schema": "auction_finalize_v2", "asof": event_asof, "source_asof": "2026-06-10", "shortlist": []},
    )

    result = oc.build_confirmation([], event_asof, limit=1)

    assert result["discipline_state"]["consecutive_losses"] == 3
    assert result["discipline_state"]["blocked"] is True
    assert "consecutive_losses_freeze" in result["discipline_state"]["reasons"]
    report = oc.json_report(result)
    assert report["discipline_state"]["blocked"] is True
