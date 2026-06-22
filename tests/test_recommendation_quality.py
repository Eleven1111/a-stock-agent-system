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
    assert "announcement_thesis_invalidated" in report["blocking_checks"]
    assert any("澄清" in risk for risk in report["risk_warnings"])


def test_generic_abnormal_volatility_notice_is_warning_not_automatic_veto():
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[{
            "title": "股票交易异常波动公告",
            "text": "公司经营情况正常，不存在应披露而未披露的重大事项。",
        }],
        asof=date(2026, 6, 12),
    )

    assert report["status"] == "passed"
    assert report["announcement_scan"]["warning_only_hits"] == ["异常波动"]
    assert "announcement_thesis_invalidated" not in report["blocking_checks"]


def test_procedural_regulatory_notice_requires_review_without_claiming_hard_risk():
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[{
            "title": "关于收到监管问询函的公告",
            "text": "公司将在规定期限内回复。",
        }],
        asof=date(2026, 6, 12),
    )

    assert report["status"] == "conditional"
    assert "announcement_review_required" in report["blocking_checks"]
    assert "announcement_hard_risk" not in report["blocking_checks"]


def test_disclosed_hard_risk_still_rejects_recommendation():
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[{
            "title": "关于公司被立案调查的公告",
            "text": "公司收到中国证监会立案告知书。",
        }],
        asof=date(2026, 6, 12),
    )

    assert report["status"] == "rejected"
    assert "announcement_hard_risk" in report["blocking_checks"]


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


def test_execution_plan_infers_atr_and_daban_lane_from_candidate():
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[],
        asof=date(2026, 6, 12),
    )

    plan = quality.build_execution_plan(
        {
            "price": 10.0,
            "prev_close": 9.8,
            "action": "trend_watch",
            "atr14": 0.4,
            "auction_selected_by": {"daban": True, "trend": False},
            "tradeability": {"tradeable": True},
        },
        report,
        asof=date(2026, 6, 12),
        stage="auction",
    )

    assert plan["strategy_lane"] == "daban"
    assert plan["pricing_method"] == "atr_adaptive"
    assert plan["stop_price"] == 9.52
    assert plan["target_price"] == 10.8
    assert plan["horizon"] == "T+1"


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


def test_missing_market_intelligence_downgrades_buy_to_conditional():
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[],
        asof=date(2026, 6, 12),
    )

    merged = quality.merge_market_intelligence(
        report,
        {
            "available": False,
            "directional_ready": False,
            "missing_datasets": ["lockups", "margin_trading", "holder_changes"],
            "hard_risks": [],
            "warnings": [],
        },
    )

    assert merged["status"] == "conditional"
    assert merged["eligible_for_directional_advice"] is False
    assert "market_intelligence_missing" in merged["blocking_checks"]


def test_stale_required_market_intelligence_downgrades_buy_to_conditional():
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[],
        asof=date(2026, 6, 12),
    )

    merged = quality.merge_market_intelligence(
        report,
        {
            "available": True,
            "directional_ready": False,
            "stale_datasets": ["margin_trading"],
            "hard_risks": [],
            "warnings": ["stale_dataset:margin_trading"],
        },
    )

    assert merged["status"] == "conditional"
    assert merged["eligible_for_directional_advice"] is False
    assert "market_intelligence_incomplete" in merged["blocking_checks"]
