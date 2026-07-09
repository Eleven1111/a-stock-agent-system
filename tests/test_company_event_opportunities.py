import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "skills" / "common"
sys.path.insert(0, str(COMMON))

from company_event_opportunities import scan_company_event_opportunities
from company_event_schema import make_opportunity


def test_scan_classifies_events_without_fabricating_probabilities():
    result = scan_company_event_opportunities(
        targets=[{"code": "600000", "name": "浦发银行"}],
        source_payloads=[{
            "source": "unit",
            "events": [{
                "code": "600000",
                "title": "浦发银行公告拟回购股份",
                "published_at": "2026-07-07",
            }],
        }],
        trading_date="2026-07-07",
        batch_id="a-share-20260707",
    )

    item = result["opportunities"][0]
    assert result["schema"] == "company_event_opportunities_v1"
    assert item["event_type"] == "buyback_increase"
    assert item["suggestion"] == "watch"
    assert item["directional_ready"] is False
    assert item["upside_pct"] is None
    assert item["success_probability"] is None
    assert item["downside_pct"] is None
    assert "success_probability_evidence_unavailable" in item["risk_flags"]


def test_expected_value_only_when_all_inputs_exist():
    item = make_opportunity(
        code="000001",
        name="平安银行",
        event_type="buyback_increase",
        evidence=[{"title": "回购"}],
        upside_pct=10.0,
        downside_pct=-4.0,
        success_probability=0.5,
    )

    assert item["expected_value_pct"] == 3.0

