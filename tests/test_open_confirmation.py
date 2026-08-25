"""09:35 early open confirmation pure decision tests."""

import importlib.util
from pathlib import Path

import candidate_lifecycle
import pytest
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
        "auction_volume": 20_000,
        "prev_day_volume": 1_000_000,
        "matched": 2_000_000,
        "unmatched": 0,
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
        "auction_volume": 20_000,
        "prev_day_volume": 1_000_000,
        "matched": 2_000_000,
        "unmatched": 0,
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


def test_open_confirmation_blocks_daban_candidate_without_hot_money_qualified():
    # 游资门禁走 hot_money_qualified（lane 成员判定），而非通用 selection_context.qualified：
    # 缺乏游资资格的打板候选不得占用打板名额，也不因通用质量门禁误伤趋势车道。
    shortlist = [
        {
            "code": "sh600001",
            "name": "打板不合格",
            "auction_score": 99,
            "auction_daban_score": 99,
            "auction_trend_score": 10,
            "auction_selected_by": {"daban": True, "trend": False},
            "hot_money_qualified": False,
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
                "sector": "半导体",
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
    monkeypatch.setattr(oc.monitor_registry, "MIRROR_LEDGER_FILE", str(tmp_path / "monitor_ledger.jsonl"))
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
            "auction_volume": 20_000,
            "prev_day_volume": 1_000_000,
            "matched": 2_000_000,
            "unmatched": 0,
            "board_status": "high_open",
            "is_yiziban": False,
            "strict_execution": True,
            "decision_mode": "live",
            "point_in_time": {
                "schema": "pit_stage_contract_v1",
                "decision_mode": "live",
                "event_asof": event_asof,
                "evidence_time": f"{event_asof}T09:34:00+08:00",
                "captured_at": f"{event_asof}T09:35:00+08:00",
                "stage_policy": {
                    "schema": "pit_stage_contract_v1",
                    "stage": "open_confirmation",
                    "cutoff_time": "09:35:00",
                    "timezone": "Asia/Shanghai",
                    "publication_delay_seconds": 0,
                },
            },
            "listing_date": "2020-01-01",
            "listing_stage": "normal",
            "is_st": False,
        },
        {
            "code": "sz300001",
            "name": "趋势候选",
            "auction_score": 80,
            "auction_daban_score": 0,
            "auction_trend_score": 80,
            "auction_selected_by": {"daban": False, "trend": True},
            "auction_gap_pct": 2.0,
            "auction_volume": 20_000,
            "prev_day_volume": 1_000_000,
            "matched": 2_000_000,
            "unmatched": 0,
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
                "volume": 100_000,
                    "change_pct": 5.0,
                    "directional_eligible": True,
            }
            for code in codes
        }

    monkeypatch.setattr(oc, "fetch_tencent_snapshot", _quotes)

    result = oc.build_confirmation([], event_asof, limit=2)

    assert "sh600001" in requested
    assert result["market_temperature"]["retreat_signal"]
    assert result["market_temperature"]["allow_new_daban"] is False
    assert [item["code"] for item in result["signals"]] == ["sz300001"]


def test_degraded_shortlist_blocks_new_risk_instead_of_reading_as_no_opportunity(
    tmp_path, monkeypatch
):
    """竞价采集为空产出的 degraded 短名单，不能被当成"今天没有机会"照常放行。

    空短名单 + 新鲜梯队时，退潮检查仍会跑（用的是梯队码，不依赖竞价），
    所以温度会正常算出档位并发放风险预算——降级信息若不透传就彻底丢失。
    """
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(oc.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(oc.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(oc.monitor_registry, "MIRROR_LEDGER_FILE", str(tmp_path / "monitor_ledger.jsonl"))
    monkeypatch.setattr(oc.recommendation_audit, "RECOMMENDATIONS_FILE", str(tmp_path / "recommendations.json"))
    monkeypatch.setattr(oc.recommendation_audit, "HISTORY_FILE", str(tmp_path / "trade_history.json"))
    monkeypatch.setattr(oc.recommendation_audit, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(oc.recommendation_audit, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(oc, "scan_many", lambda codes: {str(code)[-6:]: [] for code in codes})
    source_asof = "2026-06-10"
    event_asof = "2026-06-11"
    atomic_write_json(
        oc._shortlist_path(event_asof),
        {
            "schema": "auction_finalize_v2",
            "asof": event_asof,
            "source_asof": source_asof,
            "status": "degraded",
            "collection_status": "empty",
            "research_only": True,
            "degraded_reasons": ["竞价采集为空（0 只标的），无盘中观测，拒绝输出可执行结论"],
            "shortlist": [],
        },
    )
    # 梯队新鲜且无退潮 → 温度本会算出正常档位并放行
    monkeypatch.setattr(
        oc,
        "read_signal_context",
        lambda **_kwargs: {
            "ladder_asof": source_asof,
            "lianban_ladder": {
                "600001": {"lianban": 5},
                "000002": {"lianban": 2},
                "000003": {"lianban": 1},
                "000004": {"lianban": 1},
                "000005": {"lianban": 1},
            },
            "prev_lianban_ladder": {
                "600001": {"lianban": 4},
                "000002": {"lianban": 1},
                "000003": {"lianban": 1},
                "000004": {"lianban": 1},
                "000005": {"lianban": 1},
            },
        },
    )
    monkeypatch.setattr(
        oc,
        "fetch_tencent_snapshot",
        lambda codes: {
            code: {
                "name": code, "price": 10.5, "prev_close": 10.0, "open": 10.4,
                "high": 10.6, "low": 10.3, "volume": 100_000,
                "change_pct": 5.0, "directional_eligible": True,
            }
            for code in codes
        },
    )

    result = oc.build_confirmation([], event_asof, limit=2)

    temperature = result["market_temperature"]
    assert temperature["retreat_signal"] is None       # 确实没有退潮证据
    assert temperature["allow_new_daban"] is False     # 但仍须阻断
    assert temperature["position_multiplier"] == 0.0
    assert temperature["top_n_limit"] == 0
    assert temperature["context_status"] == "degraded"
    assert result["status"] == "degraded"
    assert any("竞价短名单降级" in note for note in temperature.get("notes") or [])


def test_build_confirmation_persists_top_signals_and_lifecycle(tmp_path, monkeypatch):
    import market_temperature

    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(oc.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(oc.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(oc.monitor_registry, "MIRROR_LEDGER_FILE", str(tmp_path / "monitor_ledger.jsonl"))
    monkeypatch.setattr(oc.recommendation_audit, "RECOMMENDATIONS_FILE", str(tmp_path / "recommendations.json"))
    monkeypatch.setattr(oc.recommendation_audit, "HISTORY_FILE", str(tmp_path / "trade_history.json"))
    monkeypatch.setattr(oc.recommendation_audit, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(oc.recommendation_audit, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(
        oc.recommendation_audit.PORTFOLIO_FILE,
        {"cash": 200000, "positions": [], "cash_reconciled": True},
    )
    atomic_write_json(
        str(tmp_path / "skills" / "stock-triage" / "data" / "portfolio.json"),
        {"cash": 200000, "positions": [], "cash_reconciled": True},
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
    monkeypatch.setattr(
        oc,
        "read_market_context",
        lambda: {
            "status": "ok",
            "context_status": "fresh",
            "context_fresh": True,
            "sector_impact": {},
            "alerts": [],
        },
    )
    monkeypatch.setattr(
        oc.recommendation_audit,
        "read_market_context",
        oc.read_market_context,
    )
    monkeypatch.setattr(
        market_temperature,
        "read_temperature",
        lambda **_kwargs: {
            "tier": "发酵",
            "context_status": "fresh",
            "context_fresh": True,
            "allow_new_daban": True,
            "position_multiplier": 1.0,
        },
    )
    source_asof = "2026-06-10"
    event_asof = "2026-06-11"
    shortlist = [
        {
            "code": f"sh600{i:03d}",
            "name": f"股票{i}",
            "sector": "半导体",
            "auction_score": 90 - i,
            "daban_score": 85 - i,
            "trend_score": 70 + i,
            "auction_gap_pct": 2.0,
            "auction_volume": 20_000,
            "prev_day_volume": 1_000_000,
            "matched": 2_000_000,
            "unmatched": 0,
            "board_status": "high_open",
            "is_yiziban": False,
            "strict_execution": True,
            "decision_mode": "live",
            "point_in_time": {
                "schema": "pit_stage_contract_v1",
                "decision_mode": "live",
                "event_asof": event_asof,
                "evidence_time": f"{event_asof}T09:34:00+08:00",
                "captured_at": f"{event_asof}T09:35:00+08:00",
                "stage_policy": {
                    "schema": "pit_stage_contract_v1",
                    "stage": "open_confirmation",
                    "cutoff_time": "09:35:00",
                    "timezone": "Asia/Shanghai",
                    "publication_delay_seconds": 0,
                },
            },
            "listing_date": "2020-01-01",
            "listing_stage": "normal",
                "is_st": False,
                "portfolio_risk_evidence": {
                    "schema": "portfolio_risk_evidence_v2",
                    "asof": event_asof,
                    "data_cutoff": "2026-06-10",
                    "proposed_position_pct": 25.0,
                    "source": "risk-engine-fixture",
                    "coverage": 1.0,
                    "correlation": 0.35,
                    "beta": 1.05,
                    "style_exposure_pct": 22.0,
                    "adv_participation_pct": 3.0,
                    "portfolio_volatility_pct": 18.0,
                },
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
                "volume": 100_000,
                "change_pct": 5.0,
                "directional_eligible": True,
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
            # 本用例验的是信号落盘与生命周期，不是 serenity 门禁。深研证据必须
            # 显式给「有且不过期」，否则会被 serenity_evidence_missing 降级为
            # watch，signal.opened 归零 —— 缺证据不再等于通过。
            "serenity": {
                "available": True,
                "stale": False,
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
    assert any(event["event_type"].startswith("monitor.") for event in events)
    monitor_events = oc.monitor_registry.monitor_ledger.read_events(
        oc.monitor_registry.MIRROR_LEDGER_FILE
    )
    assert sum(event["event_type"] == "monitor.activated" for event in monitor_events) == 4
    assert any(
        event["payload"].get("kind") == "sector"
        and event["payload"].get("key") == "半导体"
        for event in monitor_events
    )
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
        "sector": "半导体",
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
    monkeypatch.setattr(oc.monitor_registry, "MIRROR_LEDGER_FILE", str(tmp_path / "monitor_ledger.jsonl"))
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


def test_open_confirmation_flags_unreliable_indicative_price():
    """issue #140 P2：竞价指示价与实际开盘价偏差 >2% → 竞价信号标记不可信。"""
    factor = {
        "code": "sz002212",
        "name": "天融信",
        "auction_gap_pct": 0.0,
        "auction_volume": 20_000,
        "prev_day_volume": 1_000_000,
        "matched": 2_000_000,
        "unmatched": 0,
        "indicative_price": 6.60,
        "board_status": "flat_or_low_open",
        "is_yiziban": False,
    }
    quote = {
        "price": 6.41, "prev_close": 6.60, "open": 6.41,
        "low": 6.40, "high": 6.45, "volume": 1000, "change_pct": -2.88,
    }

    result = oc.evaluate_open_confirmation(factor, quote)

    assert result["auction_indicative_reliable"] is False
    assert result["auction_indicative_deviation_pct"] == -2.88
    assert any("不可信" in reason for reason in result["reasons"])


def test_open_confirmation_keeps_indicative_price_reliable_when_close():
    factor = {
        "code": "sz002156",
        "name": "通富微电",
        "auction_gap_pct": 4.0,
        "auction_volume": 20_000,
        "prev_day_volume": 1_000_000,
        "matched": 2_000_000,
        "unmatched": 0,
        "indicative_price": 10.4,
        "board_status": "high_open",
        "is_yiziban": False,
    }
    quote = {
        "price": 10.5, "prev_close": 10.0, "open": 10.4,
        "low": 10.3, "high": 10.6, "volume": 1000, "change_pct": 5.0,
    }

    result = oc.evaluate_open_confirmation(factor, quote)

    assert result["auction_indicative_reliable"] is True
    assert result["auction_indicative_deviation_pct"] == 0.0
    assert result["action"] == "trend_watch"


def test_derive_open_metrics_reads_direct_fields():
    """Every branch of the helper must resolve; a bare ``direct`` name broke all of them."""
    factor = {
        "previous_volume": 1000,
        "sector_limitup_count": 6,
        "sector_breakout_count": 3,
        "seal_persistence": 0.8,
    }
    quote = {
        "volume": 2500,
        "vwap_above_time_ratio": 0.75,
        "open_15m_drawdown_pct": 1.2,
        "open_30m_drawdown_pct": 2.4,
    }

    metrics = oc.derive_open_metrics(factor, quote)

    assert metrics["open_relative_volume"] == 2.5
    assert metrics["vwap_above_time_ratio"] == 0.75
    assert metrics["open_15m_drawdown_pct"] == 1.2
    assert metrics["open_30m_drawdown_pct"] == 2.4
    assert metrics["sector_limitup_diffusion"] == 6.0
    assert metrics["sector_breakout_diffusion"] == 3.0
    assert metrics["seal_persistence"] == 0.8


def test_derive_open_metrics_keeps_data_gaps_as_none():
    metrics = oc.derive_open_metrics({}, {})

    assert metrics["open_relative_volume"] is None
    assert metrics["vwap_above_time_ratio"] is None
    assert metrics["open_15m_drawdown_pct"] is None
    assert metrics["sector_limitup_diffusion"] is None
    assert metrics["seal_persistence"] is None


def test_five_minute_observation_does_not_claim_fifteen_minute_metrics():
    minute_bars = [
        {
            "time": f"09{minute:02d}",
            "price": 10.0 + minute / 100,
            "cum_volume": 1_000 * (minute - 29),
            "cum_amount": 10_000 * (minute - 29),
        }
        for minute in range(30, 36)
    ]

    metrics = oc.derive_open_metrics({}, {"minute_bars": minute_bars})

    assert metrics["observed_bar_count"] == 6
    assert metrics["elapsed_minutes"] == 5
    assert metrics["observation_stage"] == "early_5m"
    assert metrics["open_15m_drawdown_pct"] is None
    assert metrics["open_30m_drawdown_pct"] is None


def test_gapped_minute_rows_do_not_claim_complete_window():
    rows = [
        {"time": f"{(570 + index) // 60:02d}{(570 + index) % 60:02d}",
         "price": 10 + index / 100, "vwap": 10}
        for index in range(14)
    ] + [{"time": "1000", "price": 11.0, "vwap": 10.0}]

    metrics = oc.derive_open_metrics({}, {"minute_bars": rows})

    assert metrics["observed_bar_count"] == 15
    assert metrics["metric_coverage"]["fifteen_minute_ready"] is False
    assert metrics["open_15m_drawdown_pct"] is None


def test_score_exposes_raw_and_live_values_without_bypassing_trend_gate():
    result = oc._score_confirmation(
        {
            "code": "sz000039",
            "action": "trend_watch",
            "change_pct": 3.6,
            "tradeability": {"tradeable": True},
            "minute_bars": [
                {"time": "0930", "price": 9.78, "vwap": 9.78},
                {"time": "0935", "price": 9.21, "vwap": 9.50},
            ],
        },
        {
            "code": "sz000039",
            "auction_score": 90.0,
            "auction_trend_score_raw": 90.0,
            "auction_selected_by": {"daban": False, "trend": True},
        },
        0.8,
    )

    assert result["open_score_raw"] > 0
    assert result["open_score_live"] == 0.0
    assert result["open_score"] == result["open_score_live"]
    assert result["trend_live_weight"] == 0.0


@pytest.mark.parametrize(
    ("count", "ready_15", "ready_30"),
    [(14, False, False), (15, True, False), (29, True, False), (30, True, True)],
)
def test_open_metric_windows_require_complete_distinct_bars(count, ready_15, ready_30):
    rows = [
        {"time": f"{(570 + index) // 60:02d}{(570 + index) % 60:02d}",
         "price": 10 + index / 100, "vwap": 10}
        for index in range(count)
    ]

    metrics = oc.derive_open_metrics({}, {"minute_bars": rows})

    assert (metrics["open_15m_drawdown_pct"] is not None) is ready_15
    assert (metrics["open_30m_drawdown_pct"] is not None) is ready_30


def test_json_report_preserves_raw_and_live_score_audit_fields():
    report = oc.json_report({
        "schema": "open_confirmation_v3",
        "status": "ready",
        "signals": [{
            "code": "sz000039",
            "open_score": 0.0,
            "open_score_raw": 78.27,
            "open_score_live": 0.0,
            "score_status": "research_only",
            "metric_coverage": {"fifteen_minute_ready": False},
        }],
    })

    assert report["signals"][0]["open_score_raw"] == 78.27
    assert report["signals"][0]["open_score_live"] == 0.0
    assert report["signals"][0]["score_status"] == "research_only"


def test_loads_same_day_precomputed_portfolio_risk_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    asof = "2026-06-22"
    atomic_write_json(
        oc._portfolio_risk_evidence_path(asof),
        {"asof": asof, "evidence_by_code": {"000039": {"schema": "portfolio_risk_evidence_v2"}}},
    )

    evidence = oc.load_portfolio_risk_evidence(asof)

    assert evidence["000039"]["schema"] == "portfolio_risk_evidence_v2"


def test_open_snapshot_fetches_minutes_only_for_bounded_shortlist(monkeypatch):
    requested = []
    monkeypatch.setattr(
        oc,
        "fetch_tencent_snapshot",
        lambda codes: {code: {"price": 10.0} for code in codes},
    )
    monkeypatch.setattr(
        oc,
        "fetch_tencent_minute",
        lambda code, *, market: requested.append((market, code)) or [
            {
                "time": f"09{minute:02d}",
                "price": 10.0,
                "cum_volume": 100 * (minute - 29),
                "cum_amount": 100_000 * (minute - 29),
            }
            for minute in range(30, 36)
        ],
    )

    quotes = oc._fetch_snapshots(
        ["sh600001", "sz000002", "sh600003"],
        asof=oc.date.today().isoformat(),
        minute_codes=["sh600001", "sz000002"],
    )

    assert sorted(requested) == [("sh", "600001"), ("sz", "000002")]
    assert "minute_bars" in quotes["sh600001"]
    assert "minute_bars" not in quotes["sh600003"]


def test_open_snapshot_degrades_when_minute_provider_fails(monkeypatch):
    monkeypatch.setattr(
        oc,
        "fetch_tencent_snapshot",
        lambda codes: {code: {"price": 10.0} for code in codes},
    )
    monkeypatch.setattr(
        oc,
        "fetch_tencent_minute",
        lambda code, *, market: (_ for _ in ()).throw(
            oc.DataSourceError("tencent_minute", "unavailable")
        ),
    )

    quotes = oc._fetch_snapshots(
        ["sh600001"],
        asof=oc.date.today().isoformat(),
        minute_codes=["sh600001"],
    )

    assert "sh600001" not in quotes


def test_historical_open_live_fetch_is_blocked():
    with pytest.raises(oc.DataSourceError, match="replay"):
        oc._require_same_day_live("2026-06-22")


def test_open_cutoff_reconstruction_requires_complete_sorted_minutes():
    complete = [
        {
            "time": f"09{minute:02d}",
            "price": 10 + minute / 100,
            "cum_volume": 100 * (minute - 29),
            "cum_amount": 100_000 * (minute - 29),
        }
        for minute in range(35, 29, -1)
    ]

    rebuilt = oc._reconstruct_quote_at_cutoff(
        {"price": 19.9, "volume": 999_999, "prev_close": 10.0},
        complete,
        cutoff="0935",
    )
    incomplete = oc._reconstruct_quote_at_cutoff(
        {"price": 19.9, "volume": 999_999}, complete[:-1], cutoff="0935"
    )

    assert rebuilt["price"] == 10.35
    assert rebuilt["volume"] == 600
    assert rebuilt["evidence_cutoff"] == "0935"
    assert incomplete == {}


def test_research_only_shortlist_emits_no_signals_even_when_candidates_present(
    tmp_path, monkeypatch
):
    """research_only 的短名单必须一条信号都不产出——这是显式 fail-closed 安全网。

    实际链路里 research_only 与空短名单同时出现（弱市门禁清零候选池），signals
    本就为空；本用例刻意喂一份**非空**短名单来单独钉住安全网自身的契约，否则
    这段代码永远不会被任何断言覆盖，将来有别的生产方设置 research_only 时会
    静默失效。
    """
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(oc.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(oc.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(oc.monitor_registry, "MIRROR_LEDGER_FILE", str(tmp_path / "monitor_ledger.jsonl"))
    monkeypatch.setattr(oc.recommendation_audit, "RECOMMENDATIONS_FILE", str(tmp_path / "recommendations.json"))
    monkeypatch.setattr(oc.recommendation_audit, "HISTORY_FILE", str(tmp_path / "trade_history.json"))
    monkeypatch.setattr(oc.recommendation_audit, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(oc.recommendation_audit, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(oc, "scan_many", lambda codes: {str(code)[-6:]: [] for code in codes})
    source_asof = "2026-06-10"
    event_asof = "2026-06-11"
    atomic_write_json(
        oc._shortlist_path(event_asof),
        {
            "schema": "auction_finalize_v2",
            "asof": event_asof,
            "source_asof": source_asof,
            "status": "ready",
            "research_only": True,
            "shortlist": [{
                "code": "sz300001",
                "name": "趋势候选",
                "auction_score": 80,
                "auction_daban_score": 0,
                "auction_trend_score": 80,
                "auction_selected_by": {"daban": False, "trend": True},
                "auction_gap_pct": 2.0,
                "board_status": "high_open",
                "is_yiziban": False,
            }],
        },
    )
    monkeypatch.setattr(
        oc,
        "read_signal_context",
        lambda **_kwargs: {
            "ladder_asof": source_asof,
            "lianban_ladder": {f"00000{i}": {"lianban": 1} for i in range(1, 6)},
            "prev_lianban_ladder": {f"00000{i}": {"lianban": 1} for i in range(1, 6)},
        },
    )
    monkeypatch.setattr(
        oc,
        "fetch_tencent_snapshot",
        lambda codes: {
            code: {
                "name": code, "price": 10.5, "prev_close": 10.0, "open": 10.4,
                "high": 10.6, "low": 10.3, "volume": 100_000,
                "change_pct": 5.0, "directional_eligible": True,
            }
            for code in codes
        },
    )

    result = oc.build_confirmation([], event_asof, limit=2)

    assert result["research_only"] is True
    assert result["signals"] == []
    assert result["signal_count"] == 0


# ---------------------------------------------------------------------------
# issue #260 §4.C: 09:35 third confirmation for local_theme conditional path.
# ---------------------------------------------------------------------------


def _oc_local_theme_member(code, *, tradeable_status="limit_up", auction_sector_rank=1):
    return {
        "code": code,
        "quote_available": True,
        "tradeability_status": tradeable_status,
        "risk_hard_block": False,
        "local_theme_gate": {
            "sector": "贵金属",
            "evidence_types": ["breadth", "limitup_cluster", "sector_flow"],
        },
        "auction_sector_rank": auction_sector_rank,
    }


def test_open_local_theme_evidence_counts_limit_up_and_sealed_as_strong():
    members = [
        _oc_local_theme_member("600001", tradeable_status="limit_up"),
        _oc_local_theme_member("600002", tradeable_status="limit_up_sealed"),
        _oc_local_theme_member("600003", tradeable_status="limit_up"),
        _oc_local_theme_member("600004", tradeable_status="flat_or_low_open"),
    ]

    strong_codes, evidence_types = oc._open_local_theme_evidence(members, min_strong_members=3)

    assert strong_codes == ["600001", "600002", "600003"]
    assert evidence_types == ["breadth", "limitup_cluster"]


def test_open_local_theme_evidence_below_threshold_has_no_breadth_evidence():
    members = [
        _oc_local_theme_member("600001", tradeable_status="limit_up"),
        _oc_local_theme_member("600002", tradeable_status="flat_or_low_open"),
    ]

    strong_codes, evidence_types = oc._open_local_theme_evidence(members, min_strong_members=3)

    assert strong_codes == ["600001"]
    assert evidence_types == []


def test_reconfirm_open_sector_gate_confirms_with_fresh_strong_members():
    members = [_oc_local_theme_member(f"60000{i}") for i in range(1, 5)]

    gate = oc._reconfirm_open_sector_gate("贵金属", members, config={})

    assert gate["resonance_status"] == "confirmed"
    assert gate["execution_risk_status"] == "clear"
    assert gate["confirmation_level"] == "open"


def test_reconfirm_open_sector_gate_single_hard_risk_member_does_not_block_sector():
    """issue #260 §4.C.10：单只硬风险只阻断该候选自己，剔除后剩 3 只仍够，
    板块级 execution_risk_status 不因这一只票而整体 blocked。"""
    members = [_oc_local_theme_member(f"60000{i}") for i in range(1, 5)]
    members[0]["risk_hard_block"] = True

    gate = oc._reconfirm_open_sector_gate("贵金属", members, config={})

    assert gate["resonance_status"] == "confirmed"
    assert gate["execution_risk_status"] == "clear"
    # 风险成员自身被剔除，强势成员数只统计剩余 3 只。
    assert gate["strong_member_count"] == 3


def test_reconfirm_open_sector_gate_degrades_when_risk_removal_breaks_minimum():
    """风险成员剔除后剩余不足 min_strong_members：板块结构本身随之降级，
    而不是靠一个单独的"板块级风险"字段来表达。"""
    members = [_oc_local_theme_member(f"60000{i}") for i in range(1, 5)]
    members[0]["risk_hard_block"] = True
    members[1]["risk_hard_block"] = True

    gate = oc._reconfirm_open_sector_gate("贵金属", members, config={})

    assert gate["resonance_status"] != "confirmed"


def test_reconfirm_open_sector_gate_single_survivor_is_pulse_not_observed():
    """9:35 只剩 1 只强势成员：单票脉冲固定为 none，不是 observed。"""
    members = [
        _oc_local_theme_member("600001", tradeable_status="limit_up"),
        *[_oc_local_theme_member(f"60000{i}", tradeable_status="flat_or_low_open") for i in range(2, 5)],
    ]

    gate = oc._reconfirm_open_sector_gate("贵金属", members, config={})

    assert gate["resonance_status"] == "none"


def _local_theme_conditional_candidate(code, *, auction_sector_rank=1, asof="2026-08-24"):
    return {
        "code": code,
        "name": f"股票{code}",
        "sector": "贵金属",
        "daban_score": 60,
        "trend_score": 30,
        "auction_score": 70,
        "auction_sector_rank": auction_sector_rank,
        "auction_volume": 20_000,
        "prev_day_volume": 1_000_000,
        "matched": 2_000_000,
        "unmatched": 0,
        "research_only": True,
        "execution_action": "none",
        "participation_scope": "local_theme_only",
        "admission_state": "conditional_pending",
        "decision_mode": "live",
        "listing_date": "2020-01-01",
        "listing_stage": "normal",
        "is_st": False,
        "portfolio_risk_evidence": {
            "schema": "portfolio_risk_evidence_v2",
            "asof": asof,
            "data_cutoff": "2026-08-21",
            "proposed_position_pct": 25.0,
            "source": "risk-engine-fixture",
            "coverage": 1.0,
            "correlation": 0.35,
            "beta": 1.05,
            "style_exposure_pct": 22.0,
            "adv_participation_pct": 3.0,
            "portfolio_volatility_pct": 18.0,
        },
        "point_in_time": {
            "schema": "pit_stage_contract_v1",
            "decision_mode": "live",
            "event_asof": asof,
            "evidence_time": f"{asof}T09:34:00+08:00",
            "captured_at": f"{asof}T09:35:00+08:00",
            "stage_policy": {
                "schema": "pit_stage_contract_v1",
                "stage": "open_confirmation",
                "cutoff_time": "09:35:00",
                "timezone": "Asia/Shanghai",
                "publication_delay_seconds": 0,
            },
        },
        "local_theme_gate": {
            "schema": "local_theme_gate_v1",
            "sector": "贵金属",
            "resonance_status": "confirmed",
            "execution_risk_status": "pending",
            "evidence_types": ["breadth", "limitup_cluster", "sector_flow"],
        },
    }


def _local_theme_limit_up_quote():
    return {
        "price": 11.0, "prev_close": 10.0, "open": 10.8, "high": 11.0, "low": 10.7,
        "volume": 500_000, "change_pct": 10.0, "directional_eligible": True,
    }


def _wire_local_theme_policy(monkeypatch):
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
            "chanlun": {"live_bullish_signals": [], "live_bearish_signals": []},
            "market_intelligence": {
                "available": True, "stale": False, "directional_ready": True,
                "hard_risks": [], "warnings": [],
            },
        },
    )
    monkeypatch.setattr(oc, "scan_many", lambda codes: {str(code): [] for code in codes})


def test_build_local_theme_signals_confirms_conditional_buy_when_enabled(monkeypatch):
    _wire_local_theme_policy(monkeypatch)
    members = [_local_theme_conditional_candidate(f"sh60000{i}") for i in range(1, 5)]
    quotes = {item["code"]: _local_theme_limit_up_quote() for item in members}

    signals = oc.build_local_theme_signals(
        members,
        quotes=quotes,
        asof="2026-08-24",
        portfolio={"cash": 200000, "positions": []},
        regime={"regime": "neutral"},
        discipline_state={"blocked": False, "reasons": []},
        config={"enabled": True, "local_theme_conditional_trade_enabled": True},
    )

    assert len(signals) == 4
    for item in signals:
        assert item["participation_scope"] == "local_theme_only"
        assert item["admission_state"] == "conditional_ready"
        assert item["decision"] == "conditional_buy"
        assert item["local_theme_gate"]["resonance_status"] == "confirmed"


def test_open_confirmation_missing_auction_volume_fields_is_not_tradeable():
    factor = {
        "code": "sh600001",
        "name": "缺量能",
        "auction_gap_pct": 3.0,
        "auction_volume": None,
        "prev_day_volume": 1_000_000,
        "matched": 2_000_000,
        "unmatched": 0,
    }
    quote = {
        "price": 10.3,
        "prev_close": 10.0,
        "open": 10.3,
        "change_pct": 3.0,
        "volume": 100_000,
    }

    result = oc.evaluate_open_confirmation(factor, quote, asof="2026-08-24")

    assert result["action"] == "skip"
    assert any("auction_volume" in reason for reason in result["reasons"])


@pytest.mark.parametrize(
    ("field", "value"),
    [("matched", None), ("unmatched", None), ("auction_volume", 0),
     ("prev_day_volume", 0)],
)
def test_open_confirmation_rejects_each_invalid_auction_volume_field(field, value):
    factor = {
        "code": "sh600002",
        "auction_volume": 20_000,
        "prev_day_volume": 1_000_000,
        "matched": 2_000_000,
        "unmatched": 0,
    }
    factor[field] = value

    result = oc.evaluate_open_confirmation(
        factor,
        {"price": 10.3, "prev_close": 10.0, "open": 10.3, "change_pct": 3.0},
        asof="2026-08-24",
    )

    assert result["action"] == "skip"
    assert any(field in reason for reason in result["reasons"])


def test_local_theme_missing_auction_volume_fields_cannot_be_conditional_buy(monkeypatch):
    _wire_local_theme_policy(monkeypatch)
    members = [_local_theme_conditional_candidate(f"sh60007{i}") for i in range(1, 5)]
    members[0]["matched"] = None
    quotes = {item["code"]: _local_theme_limit_up_quote() for item in members}

    signals = oc.build_local_theme_signals(
        members,
        quotes=quotes,
        asof="2026-08-24",
        portfolio={"cash": 200000, "positions": []},
        regime={"regime": "neutral"},
        discipline_state={"blocked": False, "reasons": []},
        config={"enabled": True, "local_theme_conditional_trade_enabled": True},
    )

    blocked = next(item for item in signals if item["code"] == "sh600071")
    assert blocked["decision"] != "conditional_buy"
    assert blocked["execution_plan"]["decision"] == "watch"


def test_build_local_theme_signals_stays_watch_when_trade_flag_disabled(monkeypatch):
    """local_theme_conditional_trade_enabled=false：三次确认通过也只能 watch。"""
    _wire_local_theme_policy(monkeypatch)
    members = [_local_theme_conditional_candidate(f"sh60001{i}") for i in range(1, 5)]
    quotes = {item["code"]: _local_theme_limit_up_quote() for item in members}

    signals = oc.build_local_theme_signals(
        members,
        quotes=quotes,
        asof="2026-08-24",
        portfolio={"cash": 200000, "positions": []},
        regime={"regime": "neutral"},
        discipline_state={"blocked": False, "reasons": []},
        config={"enabled": True, "local_theme_conditional_trade_enabled": False},
    )

    assert len(signals) == 4
    for item in signals:
        assert item["decision"] == "watch"
        assert item["admission_state"] == "local_observed"
        # issue #260 §4.C.6：结构/风险已就绪但开关关闭——shadow 结论记下
        # "若开启会是 conditional_buy"，但不影响真实输出。
        assert item["shadow_decision"]["decision"] == "conditional_buy"


def test_build_local_theme_signals_no_shadow_when_trade_enabled(monkeypatch):
    """开关本来就打开时不需要 shadow——真实结论已经是最终答案。"""
    _wire_local_theme_policy(monkeypatch)
    members = [_local_theme_conditional_candidate(f"sh60005{i}") for i in range(1, 5)]
    quotes = {item["code"]: _local_theme_limit_up_quote() for item in members}

    signals = oc.build_local_theme_signals(
        members,
        quotes=quotes,
        asof="2026-08-24",
        portfolio={"cash": 200000, "positions": []},
        regime={"regime": "neutral"},
        discipline_state={"blocked": False, "reasons": []},
        config={"enabled": True, "local_theme_conditional_trade_enabled": True},
    )

    assert all(item["shadow_decision"] is None for item in signals)


def test_build_local_theme_signals_no_shadow_when_structurally_not_ready(monkeypatch):
    """开关关闭且结构本来就没确认——没有"若开启会怎样"可言，shadow 应为 None。"""
    _wire_local_theme_policy(monkeypatch)
    members = [_local_theme_conditional_candidate(f"sh60006{i}") for i in range(1, 5)]
    quotes = {
        members[0]["code"]: _local_theme_limit_up_quote(),
        **{
            item["code"]: {
                "price": 10.0, "prev_close": 10.0, "open": 10.0, "high": 10.1, "low": 9.9,
                "volume": 100_000, "change_pct": 0.0, "directional_eligible": True,
            }
            for item in members[1:]
        },
    }

    signals = oc.build_local_theme_signals(
        members,
        quotes=quotes,
        asof="2026-08-24",
        portfolio={"cash": 200000, "positions": []},
        regime={"regime": "neutral"},
        discipline_state={"blocked": False, "reasons": []},
        config={"enabled": True, "local_theme_conditional_trade_enabled": False},
    )

    assert all(item["shadow_decision"] is None for item in signals)


def test_build_local_theme_signals_stays_watch_when_breadth_collapses_at_open(monkeypatch):
    """9:25 观察过，但 9:35 只剩一只强势成员：结构瓦解，不得直接穿透为 conditional_buy。"""
    _wire_local_theme_policy(monkeypatch)
    members = [_local_theme_conditional_candidate(f"sh60002{i}") for i in range(1, 5)]
    quotes = {
        members[0]["code"]: _local_theme_limit_up_quote(),
        **{
            item["code"]: {
                "price": 10.0, "prev_close": 10.0, "open": 10.0, "high": 10.1, "low": 9.9,
                "volume": 100_000, "change_pct": 0.0, "directional_eligible": True,
            }
            for item in members[1:]
        },
    }

    signals = oc.build_local_theme_signals(
        members,
        quotes=quotes,
        asof="2026-08-24",
        portfolio={"cash": 200000, "positions": []},
        regime={"regime": "neutral"},
        discipline_state={"blocked": False, "reasons": []},
        config={"enabled": True, "local_theme_conditional_trade_enabled": True},
    )

    assert all(item["decision"] == "watch" for item in signals)
    assert all(item["local_theme_gate"]["resonance_status"] == "none" for item in signals)
    assert all(item["admission_state"] == "local_observed" for item in signals)


def test_build_local_theme_signals_downgrades_on_market_risk_off(monkeypatch):
    """局部路径不豁免既有 market_regime 门禁：risk_off 仍归零。"""
    _wire_local_theme_policy(monkeypatch)
    members = [_local_theme_conditional_candidate(f"sh60003{i}") for i in range(1, 5)]
    quotes = {item["code"]: _local_theme_limit_up_quote() for item in members}

    signals = oc.build_local_theme_signals(
        members,
        quotes=quotes,
        asof="2026-08-24",
        portfolio={"cash": 200000, "positions": []},
        regime={"regime": "risk_off"},
        discipline_state={"blocked": False, "reasons": []},
        config={"enabled": True, "local_theme_conditional_trade_enabled": True},
    )

    assert all(item["decision"] == "watch" for item in signals)
    assert all("market_risk_off" in item["policy_decision"]["reasons"] for item in signals)


def test_build_local_theme_signals_blocks_member_with_announcement_hard_risk(monkeypatch):
    """issue #260 §5 场景矩阵：多票共振 + 公告硬风险 → 该票 watch，仓位归零，
    不得因板块整体共振而穿透执行；其余无风险成员不受牵连。"""
    _wire_local_theme_policy(monkeypatch)
    members = [_local_theme_conditional_candidate(f"sh60004{i}") for i in range(1, 5)]
    quotes = {item["code"]: _local_theme_limit_up_quote() for item in members}
    risky_code = oc.candidate_pipeline.naked_code(members[0]["code"])
    monkeypatch.setattr(
        oc,
        "scan_many",
        lambda codes: {
            str(code): ([{"severity": "hard", "reason": "重大违规立案调查"}] if str(code) == risky_code else [])
            for code in codes
        },
    )

    signals = oc.build_local_theme_signals(
        members,
        quotes=quotes,
        asof="2026-08-24",
        portfolio={"cash": 200000, "positions": []},
        regime={"regime": "neutral"},
        discipline_state={"blocked": False, "reasons": []},
        config={"enabled": True, "local_theme_conditional_trade_enabled": True},
    )

    by_code = {oc.candidate_pipeline.naked_code(item["code"]): item for item in signals}
    risky_item = by_code[risky_code]
    assert risky_item["decision"] == "watch"
    assert (risky_item.get("execution_plan") or {}).get("position_pct") == 0.0
    # 板块整体仍可能 confirmed（3 只无风险成员足够），但该票个体风险独立阻断。
    others = [item for code, item in by_code.items() if code != risky_code]
    assert any(item["decision"] == "conditional_buy" for item in others)


def test_build_local_theme_signals_empty_when_no_conditional_candidates(monkeypatch):
    assert oc.build_local_theme_signals(
        [], quotes={}, asof="2026-08-24", portfolio={}, regime={}, discipline_state={},
    ) == []


def test_build_confirmation_produces_conditional_signals_when_research_only(tmp_path, monkeypatch):
    """issue #260 §4.C.8：顶层 research_only=True 清空普通 shortlist 信号，
    但不清空已验证 lineage 合法的 conditional_candidates。"""
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(oc.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(oc.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(oc.monitor_registry, "MIRROR_LEDGER_FILE", str(tmp_path / "monitor_ledger.jsonl"))
    monkeypatch.setattr(oc.recommendation_audit, "RECOMMENDATIONS_FILE", str(tmp_path / "recommendations.json"))
    monkeypatch.setattr(oc.recommendation_audit, "HISTORY_FILE", str(tmp_path / "trade_history.json"))
    monkeypatch.setattr(oc.recommendation_audit, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(oc.recommendation_audit, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(
        oc.recommendation_audit.PORTFOLIO_FILE,
        {"cash": 200000, "positions": [], "cash_reconciled": True},
    )
    atomic_write_json(
        str(tmp_path / "skills" / "stock-triage" / "data" / "portfolio.json"),
        {"cash": 200000, "positions": [], "cash_reconciled": True},
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
    monkeypatch.setattr(
        oc,
        "build_research_evidence",
        lambda code, strategy_id, asof: {
            "schema": "research_evidence_v1",
            "chanlun": {"live_bullish_signals": [], "live_bearish_signals": []},
            "market_intelligence": {
                "available": True, "stale": False, "directional_ready": True,
                "hard_risks": [], "warnings": [],
            },
        },
    )
    monkeypatch.setattr(oc, "scan_many", lambda codes: {str(code)[-6:]: [] for code in codes})
    monkeypatch.setattr(
        oc,
        "read_market_context",
        lambda: {
            "status": "ok", "context_status": "fresh", "context_fresh": True,
            "sector_impact": {}, "alerts": [],
        },
    )
    monkeypatch.setattr(oc.recommendation_audit, "read_market_context", oc.read_market_context)
    monkeypatch.setattr(
        oc, "_local_theme_config",
        lambda: {"enabled": True, "local_theme_conditional_trade_enabled": True, "min_strong_members": 3},
    )
    monkeypatch.setattr(
        oc.recommendation_audit,
        "_local_theme_resonance_config",
        lambda: {"local_theme_position_cap": 0.02, "local_trial_budget": 0.02},
    )

    source_asof = "2026-06-10"
    event_asof = "2026-06-11"
    conditional_candidates = [
        {
            "code": f"sh60000{i}",
            "name": f"贵金属{i}",
            "sector": "贵金属",
            "daban_score": 60,
            "trend_score": 30,
            "auction_score": 70,
            "auction_sector_rank": 1,
            "auction_volume": 20_000,
            "prev_day_volume": 1_000_000,
            "matched": 2_000_000,
            "unmatched": 0,
            "research_only": True,
            "execution_action": "none",
            "participation_scope": "local_theme_only",
            "admission_state": "conditional_pending",
            "decision_mode": "live",
            "listing_date": "2020-01-01",
            "listing_stage": "normal",
            "is_st": False,
            "portfolio_risk_evidence": {
                "schema": "portfolio_risk_evidence_v2",
                "asof": event_asof,
                "data_cutoff": source_asof,
                "proposed_position_pct": 25.0,
                "source": "risk-engine-fixture",
                "coverage": 1.0,
                "correlation": 0.35,
                "beta": 1.05,
                "style_exposure_pct": 22.0,
                "adv_participation_pct": 3.0,
                "portfolio_volatility_pct": 18.0,
            },
            "point_in_time": {
                "schema": "pit_stage_contract_v1",
                "decision_mode": "live",
                "event_asof": event_asof,
                "evidence_time": f"{event_asof}T09:34:00+08:00",
                "captured_at": f"{event_asof}T09:35:00+08:00",
                "stage_policy": {
                    "schema": "pit_stage_contract_v1",
                    "stage": "open_confirmation",
                    "cutoff_time": "09:35:00",
                    "timezone": "Asia/Shanghai",
                    "publication_delay_seconds": 0,
                },
            },
            "local_theme_gate": {
                "schema": "local_theme_gate_v1",
                "sector": "贵金属",
                "resonance_status": "confirmed",
                "execution_risk_status": "pending",
                "evidence_types": ["breadth", "limitup_cluster", "sector_flow"],
            },
        }
        for i in range(1, 5)
    ]
    atomic_write_json(
        oc._shortlist_path(event_asof),
        {
            "schema": "auction_finalize_v2",
            "asof": event_asof,
            "source_asof": source_asof,
            "status": "ready",
            "research_only": True,
            "shortlist": [],
            "conditional_candidates": conditional_candidates,
        },
    )
    monkeypatch.setattr(
        oc,
        "read_signal_context",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        oc,
        "fetch_tencent_snapshot",
        lambda codes: {
            code: {
                "name": code, "price": 11.0, "prev_close": 10.0, "open": 10.8,
                "high": 11.0, "low": 10.7, "volume": 500_000,
                "change_pct": 10.0, "directional_eligible": True,
            }
            for code in codes
        },
    )

    result = oc.build_confirmation([], event_asof, limit=2)

    assert result["research_only"] is True
    assert result["local_theme_count"] == 4
    assert result["conditional_ready_count"] == 4
    local_theme_codes = {item["code"] for item in result["local_theme_signals"]}
    assert local_theme_codes == {item["code"] for item in conditional_candidates}
    for item in result["local_theme_signals"]:
        assert item["decision"] == "conditional_buy"
        assert item["participation_scope"] == "local_theme_only"
    signal_codes = {item["code"] for item in result["signals"]}
    assert local_theme_codes <= signal_codes

    records = oc.recommendation_audit.load_recommendations()
    conditional_records = [r for r in records if r["action"] == "conditional_buy"]
    assert len(conditional_records) == 4
    for record in conditional_records:
        assert record["settleable_signal"] is False
        assert record.get("trade_id") is None
