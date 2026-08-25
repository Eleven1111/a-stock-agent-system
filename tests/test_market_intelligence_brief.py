"""User-facing morning intelligence brief formatting."""

from scripts import market_intelligence_brief as brief


def test_preopen_brief_contains_both_score_lanes():
    text = brief.format_brief(
        "preopen",
        {
            "asof": "2026-06-22",
            "scanned_count": 5207,
            "eligible_count": 3634,
            "candidate_count": 500,
            "auction_scan_count": 3634,
            "candidates": [
                {"code": "600001", "name": "打板股", "daban_score": 99, "trend_score": 20},
                {"code": "600002", "name": "趋势股", "daban_score": 10, "trend_score": 98},
            ],
        },
    )

    assert "打板评分 TOP" in text
    assert "趋势评分 TOP" in text
    assert "3634" in text


def test_preopen_weak_market_shows_research_top_and_no_execution_candidates():
    text = brief.format_brief(
        "preopen",
        {
            "asof": "2026-08-20",
            "scanned_count": 5211,
            "eligible_count": 3332,
            "candidate_count": 0,
            "auction_scan_count": 200,
            "candidates": [],
            "research_candidates": [
                {
                    "code": "600001",
                    "name": "研究候选",
                    "daban_score": 96,
                    "trend_score": 82,
                    "research_only": True,
                    "execution_action": "none",
                }
            ],
            "execution_candidates": [],
            "counts": {"research": 1, "execution": 0, "auction_scan": 200},
            "gate": {"status": "weak_market", "reasons": ["缺少主线/涨停集群/多源共振"]},
        },
    )

    assert "研究评分 TOP" in text
    assert "研究候选(600001)" in text
    assert "research_only" in text
    assert "可执行候选" in text
    assert "无" in text
    assert "缺少主线/涨停集群/多源共振" in text


def test_preopen_missing_timing_is_not_reported_as_extreme_weak_market():
    text = brief.format_brief(
        "preopen",
        {
            "asof": "2026-08-12",
            "scanned_count": 5208,
            "eligible_count": 3130,
            "candidate_count": 0,
            "auction_scan_count": 200,
            "candidates": [],
            "hot_money_selection": {
                "market_timing": {
                    "status": "insufficient_data",
                    "temperature": {"tier": "stale", "context_fresh": False},
                }
            },
        },
    )

    assert "择时证据未就绪" in text
    assert "极端弱市" not in text


def test_preopen_stale_nested_temperature_is_still_reported_as_not_ready():
    """issue #260 §2.8 回归：tier/context_fresh 必须从 temperature 子字段读取，
    不能从 market_timing 顶层读取(此前恒为空导致这条分支永远命中)。"""
    text = brief.format_brief(
        "preopen",
        {
            "asof": "2026-08-20",
            "candidate_count": 0,
            "candidates": [],
            "hot_money_selection": {
                "market_timing": {
                    "status": "ready",
                    "temperature": {"tier": "发酵", "context_fresh": True},
                    "weak_market": {"weak_regime": False},
                }
            },
        },
    )

    assert "择时证据未就绪" not in text
    assert "暂无可交付候选" in text


def test_preopen_restricted_market_gate_with_local_theme_is_distinguished():
    text = brief.format_brief(
        "preopen",
        {
            "asof": "2026-08-20",
            "candidate_count": 0,
            "candidates": [],
            "local_theme_candidates": [
                {"code": "600001", "name": "贵金属龙头", "sector": "贵金属"},
            ],
            "counts": {"research": 0, "execution": 0, "local_theme": 1, "auction_scan": 0},
            "market_gate": {
                "status": "restricted",
                "temperature_substate": "冰点杀跌",
            },
            "hot_money_selection": {
                "market_timing": {
                    "status": "ready",
                    "temperature": {"tier": "冰点", "context_fresh": True},
                    "weak_market": {"weak_regime": True},
                    "market_gate": {
                        "status": "restricted",
                        "temperature_substate": "冰点杀跌",
                    },
                }
            },
        },
    )

    assert "市场门禁：restricted" in text
    assert "局部主题观察" in text
    assert "贵金属龙头(600001)" in text
    assert "全局新增风险受限" in text


def test_preopen_blocked_market_gate_shown_when_no_data():
    text = brief.format_brief(
        "preopen",
        {
            "asof": "2026-08-20",
            "candidate_count": 0,
            "candidates": [],
            "market_gate": {"status": "blocked"},
        },
    )

    assert "市场门禁：blocked" in text


def test_open_brief_labels_the_judgement_time():
    text = brief.format_brief(
        "open",
        {
            "asof": "2026-08-12",
            "generated_at": "2026-08-12T09:35:06",
            "market_temperature": {"tier": "冰点"},
            "market_regime": {"regime": "risk_off"},
            "signals": [],
        },
    )

    assert "09:35" in text
    assert "不代表全天走势" in text


def test_preopen_unknown_temperature_tier_is_not_reported_as_weak_market():
    """选股就绪但温度档位 unknown：仍属证据未就绪，不得说成弱市门禁。"""
    text = brief.format_brief(
        "preopen",
        {
            "asof": "2026-08-12",
            "candidate_count": 0,
            "candidates": [],
            "hot_money_selection": {
                "market_timing": {
                    "status": "ready",
                    "temperature": {"tier": "unknown"},
                    "weak_market": {"weak_regime": True},
                }
            },
        },
    )

    assert "择时证据未就绪" in text
    assert "弱市门禁生效" not in text


def test_open_brief_converts_utc_timestamps_to_shanghai():
    """带 Z 的 UTC 时戳必须折算成北京时间，否则免责标注会差 8 小时。"""
    text = brief.format_brief(
        "open",
        {
            "asof": "2026-08-12",
            "generated_at": "2026-08-12T01:35:06+00:00",
            "market_temperature": {"tier": "冰点"},
            "market_regime": {"regime": "risk_off"},
            "signals": [],
        },
    )

    assert "09:35" in text
    assert "01:35" not in text


def test_open_brief_falls_back_when_the_timestamp_is_unparseable():
    text = brief.format_brief(
        "open",
        {
            "asof": "2026-08-12",
            "generated_at": "not-a-timestamp",
            "market_temperature": {"tier": "冰点"},
            "market_regime": {"regime": "risk_off"},
            "signals": [],
        },
    )

    assert "判定时点：开盘阶段" in text


def test_auction_brief_marks_pool_outsider_as_research_only():
    text = brief.format_brief(
        "auction",
        {
            "asof": "2026-06-23",
            "factors": [{"code": "sz000811", "name": "冰轮环境", "auction_gap_pct": 9.91}],
            "shortlist": [],
            "preopen_decisions": [],
        },
    )

    assert "冰轮环境" in text
    assert "池外研究情报" in text


def test_auction_brief_fills_missing_name_from_universe_quotes_cache(monkeypatch):
    monkeypatch.setattr(
        brief,
        "read_json",
        lambda path, default=None: {
            "quotes": {"600001": {"code": "600001", "name": "缓存名称"}}
        },
    )

    text = brief.format_brief(
        "auction",
        {
            "asof": "2026-08-24",
            "factors": [{"code": "sh600001", "name": "", "auction_gap_pct": 1.2}],
            "shortlist": [],
            "preopen_decisions": [],
        },
    )

    assert "缓存名称(sh600001)" in text
    assert "sh600001(sh600001)" not in text


def test_auction_brief_separates_research_scores_from_execution_candidates():
    text = brief.format_brief(
        "auction",
        {
            "asof": "2026-08-20",
            "status": "ready",
            "outcome_status": "ok_research_only",
            "reason_code": "weak_market",
            "research_count": 1,
            "execution_count": 0,
            "auction_scan_count": 200,
            "factors": [],
            "shortlist": [],
            "research_candidates": [
                {
                    "code": "sh600001",
                    "name": "竞价研究候选",
                    "auction_score": 88,
                    "research_only": True,
                }
            ],
            "execution_candidates": [],
            "gate": {"status": "weak_market", "reasons": ["弱市交付门禁未通过"]},
            "preopen_decisions": [],
        },
    )

    assert "业务结果：ok_research_only" in text
    assert "研究评分 TOP（research_only）" in text
    assert "竞价研究候选(sh600001)" in text
    assert "### 可执行候选" in text
    assert "弱市交付门禁未通过" in text


def test_auction_brief_renders_buy_sell_decisions_with_reasons():
    """打板评分之外必须给出买卖决策建议（决策 + 策略原因），不能只有分数。"""
    text = brief.format_brief(
        "auction",
        {
            "asof": "2026-08-07",
            "factors": [
                {"code": "sz002407", "name": "多氟多", "auction_gap_pct": 3.55},
            ],
            "shortlist": [
                {"code": "sz002407", "name": "多氟多", "daban_score": 94.07},
            ],
            "preopen_decisions": [
                {
                    "code": "sz002407",
                    "name": "多氟多",
                    "decision": "avoid",
                    "policy_decision": {
                        "decision": "avoid",
                        "requested_action": "conditional_buy",
                        "reasons": ["strategy_unverified", "existing_position_sector_unknown"],
                    },
                    "quality_report": {"status": "passed"},
                },
            ],
        },
    )

    assert "买卖决策建议" in text
    assert "多氟多" in text
    assert "回避" in text
    assert "策略未验证" in text
    assert "持仓板块未知" in text
    # 打板评分行也必须带决策标签
    assert "94.07｜回避" in text


def test_auction_brief_marks_unassessed_high_score_as_unassessed():
    """没有独立决策记录的高分票必须标"未评估"，不能误读成"评估过而观望"。"""
    text = brief.format_brief(
        "auction",
        {
            "asof": "2026-08-07",
            "factors": [],
            "shortlist": [
                {"code": "sh600206", "name": "有研新材", "daban_score": 92.71},
            ],
            "preopen_decisions": [],
        },
    )

    assert "92.71｜未评估" in text
    assert "观望" not in text


def test_auction_brief_warns_on_degraded_collection():
    """空榜单不能读成"今天很平静"——采集失败必须在简报里说出来。"""
    text = brief.format_brief(
        "auction",
        {
            "asof": "2026-07-20",
            "status": "degraded",
            "collection_status": "empty",
            "degraded_reasons": ["竞价采集为空（0 只标的），无盘中观测，拒绝输出可执行结论"],
            "factors": [],
            "shortlist": [],
            "preopen_decisions": [],
        },
    )

    assert "⚠️" in text
    assert "竞价采集为空" in text
    # 不得让读者以为这是一个正常的空榜
    assert "无盘中观测" in text


def test_open_brief_warns_on_degraded_and_does_not_advertise_tier_as_actionable():
    """降级时打印"温度=发酵｜无可执行信号"会被读成"市场不错，只是今天没标的"。

    实际含义是"没有观测，风险预算已归零"——比空榜单更有误导性，因为它
    主动给出了一个正面读数。
    """
    text = brief.format_brief(
        "open",
        {
            "asof": "2026-07-20",
            "status": "degraded",
            "degraded_reasons": ["竞价短名单降级（collection_status=empty）：竞价采集为空"],
            "market_temperature": {
                "tier": "发酵",
                "context_status": "degraded",
                "allow_new_daban": False,
                "position_multiplier": 0.0,
            },
            "market_regime": {"regime": "weak"},
            "signals": [],
        },
    )

    assert "⚠️" in text
    assert "竞价短名单降级" in text
    # 档位仍可显示（诊断信息），但必须同时标明风险预算已归零
    assert "新仓已阻断" in text


def test_open_brief_includes_filtered_high_score_reason():
    text = brief.format_brief(
        "open",
        {
            "asof": "2026-06-23",
            "signals": [],
            "evaluated_confirmations": [{
                "code": "sz000811",
                "name": "冰轮环境",
                "daban_score": 100,
                "action": "not_buyable",
                "tradeability": {"tradeable": False},
                "reasons": ["涨停或排队不可成交"],
            }],
        },
    )

    assert "被过滤高分票" in text
    assert "涨停或排队不可成交" in text


def test_load_stage_rejects_stale_intraday_projection(monkeypatch):
    monkeypatch.setattr(
        brief,
        "read_json",
        lambda *args, **kwargs: {"status": "ready", "asof": "2026-06-22"},
    )

    assert brief.load_stage("auction", asof="2026-06-23") == {}
    assert brief.load_stage("preopen", asof="2026-06-23")["status"] == "ready"


def test_main_prints_bounded_brief(monkeypatch, capsys):
    monkeypatch.setattr(
        brief,
        "load_stage",
        lambda stage, asof: {
            "status": "ready",
            "asof": asof,
            "candidates": [{"code": "600001", "name": "测试", "daban_score": 90}],
        },
    )
    monkeypatch.setattr(
        "sys.argv",
        ["market_intelligence_brief.py", "--stage", "preopen", "--asof", "2026-06-23"],
    )

    assert brief.main() == 0
    assert "早盘情报简报" in capsys.readouterr().out


def test_main_reports_missing_or_stale_stage_instead_of_printing_nothing(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(brief, "load_stage", lambda stage, asof: {})
    monkeypatch.setattr(
        "sys.argv",
        ["market_intelligence_brief.py", "--stage", "auction", "--asof", "2026-06-23"],
    )

    assert brief.main() == 0
    output = capsys.readouterr().out
    assert "集合竞价简报未生成" in output
    assert "上游快照缺失或过期" in output
