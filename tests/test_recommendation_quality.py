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


def test_mandatory_periodic_capital_occupation_form_is_not_a_hard_risk():
    """「非经营性资金占用及其他关联资金往来情况汇总表」是定期报告必交披露件。

    它的主题恰恰是"本期不存在非经营性资金占用"，纯子串匹配读不出这层否定。
    2026-08-27 实测：2026-08-07 候选池 237 只成分股有 86 只（36%）被判硬风险，
    抽样 25 只全部由这一条触发——定期报告季（4 月/8 月）会把接近全市场判成 avoid。
    """
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[{
            "title": "2026年半年度非经营性资金占用及其他关联资金往来情况汇总表",
            "text": "本期公司不存在非经营性资金占用及其他关联资金往来。",
        }],
        asof=date(2026, 8, 27),
    )

    assert report["status"] == "passed"
    assert report["announcement_scan"]["hard_risk_hits"] == []
    assert "announcement_hard_risk" not in report["blocking_checks"]
    # 豁免要留痕：下游审计需要看到"这条被豁免了"，而不是"从没命中过"。
    assert report["announcement_scan"]["title_exempt_hits"] == ["资金占用"]


def test_periodic_form_exemption_covers_the_real_world_title_variants():
    """同一张必交件的命名各家不一：2026-08-27 实测样本里这四种写法都真实出现过。

    逐字枚举全称必漏，所以锚点是"关联资金往来"这个披露件名 + 文体后缀。
    """
    titles = [
        "2026年半年度非经营性资金占用及其他关联资金往来情况的专项说明",
        "浙江建投2026半年度非经营性资金占用及其他关联资金往来情况表",
        "雅艺科技2026年半年度非经营性资金占用及其他关联资金往来情况表",
        "关于非经营性资金占用及其他关联资金往来情况的专项审核报告",
    ]
    for title in titles:
        report = quality.build_quality_report(
            _complete_recommendation(),
            announcements=[{"title": title}],
            asof=date(2026, 8, 27),
        )
        assert report["status"] == "passed", title
        assert report["announcement_scan"]["hard_risk_hits"] == [], title


def test_real_capital_occupation_event_still_rejects():
    """正向对照：豁免锚在"披露件类型"上，不是锚在「资金占用」这四个字上。

    没有这条，护栏会退化成"凡是提到资金占用一律放行"——比不修更危险。
    """
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[{
            "title": "关于控股股东非经营性资金占用及整改进展的公告",
            "text": "控股股东占用公司资金 2.3 亿元，公司正督促其限期归还。",
        }],
        asof=date(2026, 8, 27),
    )

    assert report["status"] == "rejected"
    assert report["announcement_scan"]["hard_risk_hits"] == ["资金占用"]
    assert "announcement_hard_risk" in report["blocking_checks"]


def test_periodic_form_exempts_only_the_capital_occupation_term():
    """同一条披露件里的**其它**硬风险词不在豁免范围内。

    这类专项说明的正文会一并说明违规担保等事项；把整条公告整体放行，等于让一条
    真信号搭定期披露件的便车被洗掉。
    """
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[{
            "title": "2026年半年度非经营性资金占用及其他关联资金往来情况的专项说明",
            "text": "本期不存在非经营性资金占用；报告期内公司存在违规担保 1.2 亿元。",
        }],
        asof=date(2026, 8, 27),
    )

    assert report["status"] == "rejected"
    assert report["announcement_scan"]["hard_risk_hits"] == ["违规担保"]
    assert report["announcement_scan"]["title_exempt_hits"] == ["资金占用"]


def test_periodic_form_exemption_does_not_whitewash_other_announcements():
    """豁免是逐条公告、逐个词的：同一只票的另一条真利空必须照旧判出来。"""
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[
            {"title": "2026年半年度非经营性资金占用及其他关联资金往来情况汇总表"},
            {"title": "关于公司被中国证监会立案调查的公告"},
        ],
        asof=date(2026, 8, 27),
    )

    assert report["status"] == "rejected"
    assert report["announcement_scan"]["hard_risk_hits"] == ["立案调查"]


def test_closed_share_reduction_plan_is_not_a_forward_looking_hard_risk():
    """「减持计划期限届满 / 终止」——计划窗口已关闭，未来不再有该计划下的减持压力。

    公告质检拦的是**前瞻**风险；纯子串匹配把"这个减持计划结束了"读成"有减持计划"。
    2026-08-27 实测 150 只抽样：命中「减持计划」的 2 只里，301503 的四条公告全是
    期限届满，属纯假阳性。taxonomy.json 的 polarity_rules.flip 早已收录同一组词
    （终止减持 / 减持期限届满未减持），共享词表此前没有对应处置。
    """
    for title in [
        "关于持股5%以上股东减持计划期限届满的公告",
        "关于特定股东、高级管理人员减持计划期限届满的公告",
        "关于终止实施减持计划的公告",
        "关于股东减持计划期满未实施的公告",
    ]:
        report = quality.build_quality_report(
            _complete_recommendation(),
            announcements=[{"title": title}],
            asof=date(2026, 8, 27),
        )
        assert report["status"] == "passed", title
        assert report["announcement_scan"]["hard_risk_hits"] == [], title


def test_live_share_reduction_plan_still_rejects():
    """正向对照：预披露的减持计划是真利空，必须照旧 avoid。

    没有这条，"计划已结束"的豁免会滑成"凡提到减持计划一律放行"。
    """
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[{"title": "关于高级管理人员减持计划的预披露公告"}],
        asof=date(2026, 8, 27),
    )

    assert report["status"] == "rejected"
    assert report["announcement_scan"]["hard_risk_hits"] == ["减持计划"]


def test_closed_plan_exemption_is_per_announcement():
    """同一只票另有在途减持计划时，届满公告不能把它洗白。

    300555 实测就是这个形态：3 条期限届满 + 1 条预披露并存。
    """
    report = quality.build_quality_report(
        _complete_recommendation(),
        announcements=[
            {"title": "关于持股5%以上股东减持计划期限届满的公告"},
            {"title": "关于特定股东未来减持计划的预披露公告"},
        ],
        asof=date(2026, 8, 27),
    )

    assert report["status"] == "rejected"
    assert report["announcement_scan"]["hard_risk_hits"] == ["减持计划"]


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
