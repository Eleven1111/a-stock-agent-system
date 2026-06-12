"""推荐审计档案测试（纯逻辑，不触网）"""

import recommendation_audit as ra
import market_temperature as mt
from state_store import atomic_write_json


def _wire(tmp_path, monkeypatch, history=None):
    monkeypatch.setattr(ra, "RECOMMENDATIONS_FILE", str(tmp_path / "recommendations.json"))
    monkeypatch.setattr(ra, "HISTORY_FILE", str(tmp_path / "trade_history.json"))
    monkeypatch.setattr(ra, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    if history is not None:
        atomic_write_json(ra.HISTORY_FILE, history)


def test_record_recommendation_writes_audit_file(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)

    result = ra.record_recommendation(
        code="2156",
        name="通富微电",
        action="buy",
        price_range="10.80-11.00",
        rationale="半导体主线早盘回封",
        risks=["T+1低开风险"],
        entry_price=11.0,
        target_price=12.1,
        stop_price=10.45,
        strategy_id="daban:first_board_reseal",
    )

    records = ra.load_recommendations()
    assert result["ok"] is True
    assert len(records) == 1
    assert records[0]["code"] == "002156"
    assert records[0]["outcome"] == "pending"
    assert records[0]["position_sizing"]["odds_b"] == 2.0


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
    result = ra.record_recommendation("002156", "通富微电", "buy", "10.80-11.00", "封测催化", risks=[])
    rec_id = result["record"]["id"]

    updated = ra.update_outcome(rec_id, "loss", pnl_pct=-3.2, note="T+1低开止损")

    assert updated["ok"] is True
    record = ra.load_recommendations()[0]
    assert record["outcome"] == "loss"
    assert record["pnl_pct"] == -3.2
    assert record["outcome_note"] == "T+1低开止损"


def test_position_guidance_uses_startup_default_when_history_insufficient(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, history=[])

    sizing = ra.position_guidance("daban:first_board_reseal", 11.0, 12.1, 10.45, total_asset=100000)

    assert sizing["method"] == "startup_default"
    assert sizing["recommended_position_pct"] == 4.0
    assert sizing["recommended_amount"] == 4000


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
