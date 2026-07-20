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
