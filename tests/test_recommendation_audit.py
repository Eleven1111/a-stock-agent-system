"""推荐审计档案测试（纯逻辑，不触网）"""

import recommendation_audit as ra
import market_temperature as mt
from state_store import atomic_write_json


def _wire(tmp_path, monkeypatch, history=None):
    monkeypatch.setattr(ra, "RECOMMENDATIONS_FILE", str(tmp_path / "recommendations.json"))
    monkeypatch.setattr(ra, "HISTORY_FILE", str(tmp_path / "trade_history.json"))
    monkeypatch.setattr(ra, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(ra, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(
        ra.PORTFOLIO_FILE,
        {"cash": 100000, "positions": [], "cash_reconciled": True},
    )
    monkeypatch.setattr(
        ra.strategy_registry,
        "live_record",
        lambda strategy_id: {
            "strategy_id": strategy_id,
            "allowed_in_live_agent": True,
            "gating_status": "enabled",
            "runtime_allowed": True,
        },
    )
    if history is not None:
        atomic_write_json(ra.HISTORY_FILE, history)


def _passed_buy(**overrides):
    values = {
        "code": "002156",
        "name": "通富微电",
        "action": "buy",
        "price_range": "10.80-11.00",
        "rationale": "半导体主线早盘回封",
        "risks": ["T+1低开风险"],
        "entry_price": 11.0,
        "target_price": 12.1,
        "stop_price": 10.45,
        "horizon": "T+1到T+3",
        "grade": "A",
        "confidence": "medium",
        "strategy_id": "daban:first_board_reseal",
        "announcements": [],
        "research_evidence": {
            "market_intelligence": {
                "available": True,
                "stale": False,
                "directional_ready": True,
                "hard_risks": [],
                "warnings": [],
            },
            "chanlun": {
                "live_bullish_signals": [],
                "live_bearish_signals": [],
            },
            "serenity": {
                "available": False,
                "stale": None,
                "hard_risks": [],
            },
        },
    }
    values.update(overrides)
    return values


def test_record_recommendation_writes_audit_file(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)

    result = ra.record_recommendation(**_passed_buy(code="2156"))

    records = ra.load_recommendations()
    assert result["ok"] is True
    assert len(records) == 1
    assert records[0]["code"] == "002156"
    assert records[0]["outcome"] == "pending"
    assert records[0]["position_sizing"]["odds_b"] == 2.0
    assert records[0]["correlation_id"]
    assert records[0]["signal_id"]
    assert records[0]["trade_id"]
    events = ra.signal_ledger.read_events(ra.LEDGER_FILE)
    assert [event["event_type"] for event in events] == [
        "recommendation.created",
        "signal.opened",
        "trade.proposed",
    ]


def test_recommendation_amount_uses_runtime_portfolio_value(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    atomic_write_json(
        ra.PORTFOLIO_FILE,
        {"cash": 12000, "positions": [{"code": "600001", "shares": 800, "current_price": 10}]},
    )

    result = ra.record_recommendation(**_passed_buy())

    sizing = result["record"]["position_sizing"]
    assert sizing["account_value"] == 20000
    assert sizing["recommended_amount"] == 400


def test_record_recommendation_preserves_selection_context_in_ledger(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    selection_context = {
        "window": "09:35",
        "market_timing": {"tier": "发酵", "daban_ready": True},
        "sector": {"name": "半导体", "rank": 1, "state": "confirmed"},
        "leader": {"rank": 1, "role": "sector_leader"},
    }

    result = ra.record_recommendation(
        **_passed_buy(selection_context=selection_context)
    )

    created = ra.signal_ledger.read_events(ra.LEDGER_FILE)[0]
    projected = ra.signal_ledger.project_signals(ledger_file=ra.LEDGER_FILE)[0]
    assert result["record"]["selection_context"] == selection_context
    assert created["payload"]["selection_context"] == selection_context
    assert projected["selection_context"] == selection_context


def test_record_recommendation_carries_chanlun_attribution_into_signal(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    evidence = {
        "chanlun": {
            "live_bullish_signals": [
                {
                    "type": "third_buy",
                    "strategy_id": "chanlun_third_buy",
                    "idx": 58,
                }
            ],
            "live_bearish_signals": [],
        },
        "serenity": {"available": False, "stale": None, "hard_risks": []},
        "market_intelligence": {
            "available": True,
            "stale": False,
            "directional_ready": True,
            "hard_risks": [],
            "warnings": [],
        },
    }

    result = ra.record_recommendation(
        **_passed_buy(research_evidence=evidence)
    )

    attribution = result["record"]["strategy_attributions"][0]
    signal = ra.signal_ledger.project_signals(ledger_file=ra.LEDGER_FILE)[0]
    assert attribution["strategy_id"] == "chanlun_third_buy"
    assert signal["strategy_attributions"] == [attribution]


def test_query_filters_by_code_and_outcome(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    ra.record_recommendation("600011", "华能国际", "buy", "9.00-9.20", "电力催化", risks=[])
    second = ra.record_recommendation("002156", "通富微电", "buy", "10.80-11.00", "封测催化", risks=[])
    ra.update_outcome(second["record"]["id"], "profit", pnl_pct=6.8)

    filtered = ra.query_recommendations(code="002156", outcome="profit")

    assert len(filtered) == 1
    assert filtered[0]["name"] == "通富微电"


def test_update_outcome_is_atomic_mutation(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    result = ra.record_recommendation(**_passed_buy(rationale="封测催化"))
    rec_id = result["record"]["id"]

    updated = ra.update_outcome(rec_id, "loss", pnl_pct=-3.2, note="T+1低开止损")

    assert updated["ok"] is True
    record = ra.load_recommendations()[0]
    assert record["outcome"] == "loss"
    assert record["pnl_pct"] == -3.2
    assert record["outcome_note"] == "T+1低开止损"
    signals = ra.signal_ledger.project_signals(ledger_file=ra.LEDGER_FILE)
    assert signals[0]["outcome"] == "loss"
    assert signals[0]["pnl_pct"] == -3.2
    assert signals[0]["settlement_id"]


def test_outcome_correction_appends_new_settlement_event(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    result = ra.record_recommendation(**_passed_buy(rationale="封测催化"))
    rec_id = result["record"]["id"]

    ra.update_outcome(rec_id, "loss", pnl_pct=-2.0)
    ra.update_outcome(rec_id, "profit", pnl_pct=4.0, note="复核更正")

    settlements = [
        event for event in ra.signal_ledger.read_events(ra.LEDGER_FILE)
        if event["event_type"] == "signal.settled"
    ]
    signals = ra.signal_ledger.project_signals(ledger_file=ra.LEDGER_FILE)
    assert len(settlements) == 2
    assert signals[0]["outcome"] == "win"
    assert signals[0]["pnl_pct"] == 4.0


def test_avoid_recommendation_does_not_open_signal(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    result = ra.record_recommendation(
        "002156",
        "通富微电",
        "avoid",
        "N/A",
        "公告风险",
        risks=["重大风险"],
    )

    ra.update_outcome(result["record"]["id"], "invalidated")

    assert ra.signal_ledger.project_signals(ledger_file=ra.LEDGER_FILE) == []


def test_hold_and_conditional_buy_do_not_open_performance_signal(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    ra.record_recommendation(
        "002156",
        "通富微电",
        "hold",
        "10.80-11.00",
        "继续观察",
        risks=[],
        announcements=[],
    )
    conditional = ra.record_recommendation(
        **_passed_buy(
            source_id="conditional-buy",
            announcements=None,
        )
    )

    events = ra.signal_ledger.read_events(ra.LEDGER_FILE)
    assert ra.signal_ledger.project_signals(events) == []
    assert conditional["record"]["quality_report"]["status"] == "conditional"
    assert conditional["record"]["settleable_signal"] is False
    assert all(event["event_type"] != "trade.proposed" for event in events)
    assert all(event["event_type"] != "trade.executed" for event in events)


def test_unregistered_strategy_is_recorded_as_watch_without_trade_or_signal(
    tmp_path,
    monkeypatch,
):
    _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(ra.strategy_registry, "live_record", lambda _strategy_id: None)

    result = ra.record_recommendation(**_passed_buy(source_id="research-only"))

    record = result["record"]
    assert record["requested_action"] == "buy"
    assert record["action"] == "hold"
    assert record["position_sizing"]["recommended_position_pct"] == 0.0
    assert "strategy_unverified" in record["policy_decision"]["reasons"]
    events = ra.signal_ledger.read_events(ra.LEDGER_FILE)
    assert [event["event_type"] for event in events] == ["recommendation.created"]


def test_position_guidance_uses_startup_default_when_history_insufficient(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, history=[])

    sizing = ra.position_guidance("daban:first_board_reseal", 11.0, 12.1, 10.45, total_asset=100000)

    assert sizing["method"] == "startup_default"
    # daban startup 默认 4.0%，叠加打板战略权重 ×0.5(#28 证伪 + 1+2 定位减仓) → 2.0%
    assert sizing["recommended_position_pct"] == 2.0
    assert sizing["recommended_amount"] == 2000


def test_position_guidance_is_zero_for_unverified_strategy(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, history=[])
    monkeypatch.setattr(ra.strategy_registry, "live_record", lambda _strategy_id: None)

    sizing = ra.position_guidance(
        "trend_pullback",
        10.0,
        12.0,
        9.0,
        total_asset=100000,
    )

    assert sizing["method"] == "research_only"
    assert sizing["recommended_position_pct"] == 0.0
    assert sizing["recommended_amount"] == 0.0


def test_daban_strategic_weight_scales_position(tmp_path, monkeypatch):
    # 打板战略权重在温度倍率之上缩放 daban 仓位：weight=0.5 应是 weight=1.0 的一半
    _wire(tmp_path, monkeypatch, history=[])
    monkeypatch.setenv("HERMES_DABAN_STRATEGIC_WEIGHT", "1.0")
    full = ra.position_guidance("daban:first_board_reseal", 11.0, 12.1, 10.45)
    monkeypatch.setenv("HERMES_DABAN_STRATEGIC_WEIGHT", "0.5")
    half = ra.position_guidance("daban:first_board_reseal", 11.0, 12.1, 10.45)
    assert full["recommended_position_pct"] > 0
    assert half["recommended_position_pct"] == round(full["recommended_position_pct"] * 0.5, 2)


def test_trend_lane_ignores_daban_strategic_weight(tmp_path, monkeypatch):
    # 趋势通道不受打板战略权重影响（只管 daban）
    _wire(tmp_path, monkeypatch, history=[])
    monkeypatch.setenv("HERMES_DABAN_STRATEGIC_WEIGHT", "0.5")
    a = ra.position_guidance("trend_pullback", 10.0, 12.0, 9.0)
    monkeypatch.setenv("HERMES_DABAN_STRATEGIC_WEIGHT", "0.1")
    b = ra.position_guidance("trend_pullback", 10.0, 12.0, 9.0)
    assert a["recommended_position_pct"] == b["recommended_position_pct"]


def test_position_guidance_uses_quarter_kelly_after_ten_trades(tmp_path, monkeypatch):
    history = [
        {"strategy_id": "trend_pullback", "pnl": 100}
        for _ in range(7)
    ] + [
        {"strategy_id": "trend_pullback", "pnl": -100}
        for _ in range(3)
    ]
    _wire(tmp_path, monkeypatch, history=history)

    sizing = ra.position_guidance("trend_pullback", 10.0, 12.0, 9.0, total_asset=100000)

    assert sizing["method"] == "kelly_quarter"
    assert sizing["history_total"] == 10
    assert sizing["history_win_rate"] == 0.7
    assert sizing["kelly_fraction"] == 0.55
    assert sizing["recommended_position_pct"] == 13.75


def test_position_guidance_blocks_new_daban_when_temperature_disallows(monkeypatch, tmp_path):
    _wire(tmp_path, monkeypatch, history=[])
    monkeypatch.setattr(
        mt,
        "read_temperature",
        lambda **kwargs: {
            "tier": "冰点",
            "allow_new_daban": False,
            "position_multiplier": 0.3,
            "context_fresh": True,
        },
    )

    sizing = ra.position_guidance(
        "daban:first_board_reseal",
        11.0,
        12.1,
        10.45,
        total_asset=100000,
    )

    assert sizing["recommended_position_pct"] == 0.0
    assert sizing["recommended_amount"] == 0.0
    assert sizing["temperature"]["allow_new_daban"] is False


def test_sell_recommendation_is_blocked_for_same_day_buy(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    atomic_write_json(
        ra.PORTFOLIO_FILE,
        {
            "positions": [{
                "code": "002156",
                "name": "通富微电",
                "shares": 1000,
                "lots": [{"shares": 1000, "acquired_on": "2026-06-12"}],
            }]
        },
    )

    result = ra.record_recommendation(
        "002156",
        "通富微电",
        "sell",
        "10.80-11.00",
        "盘中波动",
        asof="2026-06-12",
    )

    assert result["code"] == "T1_LOCKED"
    assert result["earliest_sell_date"] == "2026-06-15"
