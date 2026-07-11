"""每日执行纪律复盘 — 建议 vs 实际成交对比（纯逻辑部分不触网）"""

from state_store import atomic_write_json

import discipline_review as dr
import recommendation_audit as ra


def _wire(tmp_path, monkeypatch):
    import market_temperature

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    portfolio_path = str(tmp_path / "portfolio.json")
    monkeypatch.setattr(ra, "RECOMMENDATIONS_FILE", str(tmp_path / "recommendations.json"))
    monkeypatch.setattr(ra, "HISTORY_FILE", str(tmp_path / "trade_history.json"))
    monkeypatch.setattr(ra, "PORTFOLIO_FILE", portfolio_path)
    monkeypatch.setattr(ra, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(dr.signal_ledger, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(dr.portfolio_manager, "PORTFOLIO_FILE", portfolio_path)
    monkeypatch.setattr(dr.portfolio_manager, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(dr.portfolio_manager.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
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
    monkeypatch.setattr(
        ra,
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
    atomic_write_json(portfolio_path, {"cash": 100000, "positions": [], "cash_reconciled": True})


def _rec(**overrides):
    values = {
        "date": "2026-06-24",
        "code": "600001",
        "name": "候选票",
        "action": "buy",
        "price_range": "10.00-10.50",
        "position_sizing": {"recommended_position_pct": 4.0},
    }
    values.update(overrides)
    return values


def test_parse_price_range_handles_common_formats():
    assert dr._parse_price_range("10.80-11.00") == (10.8, 11.0)
    assert dr._parse_price_range("10.5") == (10.5, 10.5)
    assert dr._parse_price_range("N/A") == (None, None)
    assert dr._parse_price_range(None) == (None, None)


def test_review_buy_side_flags_unfollowed_recommendation():
    rows = dr.review_buy_side([_rec()], [], asof="2026-06-24", total_assets=100000)
    assert rows[0]["followed"] is False
    assert "未跟单" in rows[0]["flags"]


def test_review_buy_side_clean_execution_has_no_flags():
    trades = [{"code": "600001", "action": "open", "price": 10.3, "shares": 400, "trade_date": "2026-06-24"}]
    rows = dr.review_buy_side([_rec()], trades, asof="2026-06-24", total_assets=100000)
    assert rows[0]["followed"] is True
    assert rows[0]["flags"] == []
    assert rows[0]["actual_position_pct"] == 4.12


def test_review_buy_side_flags_chased_price_above_range():
    trades = [{"code": "600001", "action": "open", "price": 11.5, "shares": 400, "trade_date": "2026-06-24"}]
    rows = dr.review_buy_side([_rec()], trades, asof="2026-06-24", total_assets=100000)
    assert any("追价" in flag for flag in rows[0]["flags"])


def test_review_buy_side_flags_oversized_position():
    # 建议4%，实际用了远超1.3倍的仓位
    trades = [{"code": "600001", "action": "open", "price": 10.3, "shares": 3000, "trade_date": "2026-06-24"}]
    rows = dr.review_buy_side([_rec()], trades, asof="2026-06-24", total_assets=100000)
    assert any("超仓位" in flag for flag in rows[0]["flags"])


def test_review_buy_side_ignores_other_dates_and_non_buy_actions():
    recs = [_rec(date="2026-06-23"), _rec(action="hold"), _rec(action="avoid")]
    rows = dr.review_buy_side(recs, [], asof="2026-06-24", total_assets=100000)
    assert rows == []


def test_build_review_without_refresh_skips_network_and_includes_discipline_state(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    ra.record_recommendation(
        code="600001", name="候选票", action="buy", price_range="10.00-10.50",
        rationale="测试", entry_price=10.3, target_price=11.0, stop_price=9.8,
        horizon="T+1到T+3", grade="A", confidence="medium", announcements=[],
        strategy_id="daban:first_board_reseal", asof="2026-06-24", sector="半导体",
        execution_context={
            "strict_execution": True,
            "decision_mode": "live",
            "point_in_time": {
                "schema": "pit_stage_contract_v1",
                "decision_mode": "live",
                "event_asof": "2026-06-24",
                "evidence_time": "2026-06-24T14:59:00+08:00",
                "captured_at": "2026-06-24T15:00:00+08:00",
                "stage_policy": {
                    "schema": "pit_stage_contract_v1",
                    "stage": "recommendation",
                    "cutoff_time": "15:00:00",
                    "timezone": "Asia/Shanghai",
                    "publication_delay_seconds": 0,
                },
            },
            "listing_date": "2020-01-01",
            "listing_stage": "normal",
            "is_st": False,
            "direction": "buy",
            "directional_eligible": True,
            "executable_price": 10.3,
            "available_volume": 100000,
            "adv_value": 10000000,
                "corporate_action_status": "clear",
                "portfolio_risk_evidence": {
                    "schema": "portfolio_risk_evidence_v1",
                    "asof": "2026-06-24",
                    "source": "risk-engine-fixture",
                    "coverage": 1.0,
                    "correlation": 0.35,
                    "beta": 1.05,
                    "style_exposure_pct": 22.0,
                    "adv_participation_pct": 3.0,
                    "portfolio_volatility_pct": 18.0,
                },
            },
        research_evidence={
            "market_intelligence": {"available": True, "stale": False, "directional_ready": True, "hard_risks": [], "warnings": []},
            "chanlun": {"live_bullish_signals": [], "live_bearish_signals": []},
            "serenity": {"available": False, "stale": None, "hard_risks": []},
        },
    )

    review = dr.build_review("2026-06-24", refresh_prices=False)

    assert review["schema"] == "discipline_review_v1"
    assert review["pending_exit_signals"] == []
    assert len(review["buy_side"]) == 1
    assert "discipline_state" in review
    text = dr.format_report(review)
    assert "候选票" in text
