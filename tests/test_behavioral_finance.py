import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "common"))

from behavioral_finance import build_behavioral_finance_context


def test_behavioral_finance_context_degrades_without_quotes():
    result = build_behavioral_finance_context(
        {},
        {"sentiment_score": 88},
        {"limit_up_count": 30},
        {"signals": []},
        asof="2026-07-07T08:43:00",
        trading_date="2026-07-07",
        batch_id="a-share-20260707",
    )

    assert result["schema"] == "behavioral_finance_context_v1"
    assert result["sentiment_phase"] == "euphoria"
    assert result["strategy_adjustments"]["exposure_band"] == "tighten"
    assert "market_snapshot_quotes_missing" in result["unavailable"]
    assert result["has_signal"] is True


def test_behavioral_finance_agent_risk_reuses_behavior_module():
    result = build_behavioral_finance_context(
        {},
        {},
        {},
        {"signals": [
            {"signal_date": "2026-06-30", "outcome": "win", "strategy_id": "x"},
            {"signal_date": "2026-07-01", "outcome": "win", "strategy_id": "x"},
            {"signal_date": "2026-07-02", "outcome": "win", "strategy_id": "x"},
            {"signal_date": "2026-07-03", "outcome": "win", "strategy_id": "x"},
        ]},
        asof="2026-07-07T15:12:00",
    )

    assert result["agent_behavior_risk"]["win_streak"] == 4
    assert "不要因连胜扩大单笔风险预算" in result["debiasing_checklist"]

