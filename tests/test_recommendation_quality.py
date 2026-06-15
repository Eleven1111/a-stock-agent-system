from datetime import date

import recommendation_quality as quality


def _complete_recommendation():
    return {
        "code": "002156",
        "name": "通富微电",
        "action": "buy",
        "entry_price": 10.5,
        "price_range": "10.45-10.55",
        "stop_price": 9.98,
        "target_price": 11.35,
        "horizon": "T+1到T+3",
        "grade": "A",
        "confidence": "medium",
        "position_pct": 4.0,
        "tradeability": {"tradeable": True, "status": "normal"},
    }


def test_clarification_announcement_vetoes_bullish_claim():
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[
            {
                "title": "股票交易异常波动公告",
                "text": "公司澄清：未涉及AI芯片业务，相关传闻不属实，尚未形成收入。",
            }
        ],
        asof=date(2026, 6, 12),
    )

    assert report["status"] == "rejected"
    assert "announcement_clarification" in report["blocking_checks"]
    assert any("澄清" in risk for risk in report["risk_warnings"])


def test_missing_announcement_scan_cannot_be_full_pass():
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=None,
        asof=date(2026, 6, 12),
    )

    assert report["status"] == "conditional"
    assert "announcement_scan_missing" in report["blocking_checks"]


def test_complete_clean_recommendation_passes_with_t1_disclosure():
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[],
        asof=date(2026, 6, 12),
    )

    assert report["status"] == "passed"
    assert report["execution_constraints"]["same_day_sell_allowed"] is False
    assert report["execution_constraints"]["earliest_sell_date"] == "2026-06-15"


def test_execution_plan_never_emits_inverted_entry_range_above_chase_limit():
    recommendation = _complete_recommendation()
    report = quality.build_quality_report(
        recommendation,
        announcements=[],
        asof=date(2026, 6, 12),
    )

    plan = quality.build_execution_plan(
        {
            **recommendation,
            "price": 10.8,
            "prev_close": 10.0,
            "action": "trend_watch",
        },
        report,
        asof=date(2026, 6, 12),
    )

    assert plan["decision"] == "watch"
    assert plan["entry_range"] is None
    assert plan["beyond_max_chase"] is True


def test_market_intelligence_hard_risk_rejects_quality_report():
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[],
        asof=date(2026, 6, 12),
    )

    merged = quality.merge_market_intelligence(
        report,
        {
            "available": True,
            "stale": False,
            "hard_risks": ["major_lockup_within_30d"],
            "warnings": ["institutional_lhb_net_sell"],
        },
    )

    assert merged["status"] == "rejected"
    assert "market_intelligence_hard_risk" in merged["blocking_checks"]
    assert any("major_lockup_within_30d" in item for item in merged["risk_warnings"])
