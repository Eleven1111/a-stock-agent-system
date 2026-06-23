"""Bounded, research-only intelligence summaries across the morning pipeline."""

import stage_intelligence as si


def test_preopen_digest_keeps_strategy_lanes_separate():
    candidates = [
        {"code": "600001", "name": "打板一", "daban_score": 99, "trend_score": 20},
        {"code": "600002", "name": "趋势一", "daban_score": 10, "trend_score": 98},
        {"code": "600003", "name": "打板二", "daban_score": 95, "trend_score": 30},
    ]

    digest = si.preopen_digest({"candidates": candidates})

    assert [item["code"] for item in digest["top_daban"][:2]] == ["600001", "600003"]
    assert [item["code"] for item in digest["top_trend"][:2]] == ["600002", "600003"]


def test_auction_digest_surfaces_full_market_movers_without_promoting_them():
    result = {
        "factors": [
            {"code": "sh600001", "name": "池内", "auction_gap_pct": 4.0},
            {"code": "sz000811", "name": "池外涨停", "auction_gap_pct": 9.91},
            {"code": "sh600003", "name": "池外下跌", "auction_gap_pct": -7.0},
        ],
        "shortlist": [{"code": "sh600001", "daban_score": 96}],
        "preopen_decisions": [{"code": "sh600001", "name": "池内", "daban_score": 96}],
    }

    digest = si.auction_digest(result)

    assert digest["market_gainers"][0]["code"] == "sz000811"
    assert digest["market_gainers"][0]["research_only"] is True
    assert digest["market_gainers"][0]["in_execution_shortlist"] is False
    assert digest["market_decliners"][0]["code"] == "sh600003"


def test_open_digest_explains_high_score_filtering():
    result = {
        "signals": [{"code": "sh600001", "name": "入选", "open_score": 88}],
        "evaluated_confirmations": [
            {"code": "sh600001", "name": "入选", "open_score": 88},
            {
                "code": "sz000811",
                "name": "涨停排队",
                "daban_score": 100,
                "open_score": 0,
                "action": "not_buyable",
                "tradeability": {"tradeable": False, "status": "limit_up"},
                "reasons": ["涨停或排队不可成交"],
            },
        ],
    }

    digest = si.open_digest(result)

    assert digest["filtered_highlights"][0]["code"] == "sz000811"
    assert digest["filtered_highlights"][0]["filter_stage"] == "open_confirmation"
    assert "不可成交" in digest["filtered_highlights"][0]["filter_reasons"][0]
