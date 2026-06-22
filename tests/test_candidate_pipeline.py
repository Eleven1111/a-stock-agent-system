"""Dynamic full-market candidate discovery and ranking tests."""

from datetime import date, timedelta

import candidate_pipeline as cp


def _quote(code, name, change_pct, amount, turnover=5.0, price=10.0):
    return {
        "code": code,
        "name": name,
        "price": price,
        "prev_close": price / (1 + change_pct / 100),
        "change_pct": change_pct,
        "amount": amount,
        "turnover": turnover,
        "volume": 1_000_000,
        "listed_date": (date.today() - timedelta(days=500)).isoformat(),
    }


def _klines(closes, volumes=None):
    volumes = volumes or [100_000] * len(closes)
    return [
        {
            "date": f"2026-01-{i + 1:02d}",
            "open": close * 0.99,
            "close": close,
            "high": close * 1.01,
            "low": close * 0.98,
            "volume": volumes[i],
        }
        for i, close in enumerate(closes)
    ]


def test_filter_universe_returns_rejection_reasons():
    records = [
        _quote("600001", "正常股份", 2.0, 300_000_000),
        _quote("600002", "ST风险", 2.0, 300_000_000),
        {**_quote("600003", "停牌股份", 0.0, 0), "price": 0, "volume": 0},
        {**_quote("600004", "次新股份", 2.0, 300_000_000),
         "listed_date": (date.today() - timedelta(days=20)).strftime("%Y%m%d")},
        _quote("600005", "低流动性", 2.0, 20_000_000),
    ]

    eligible, rejected = cp.filter_universe(records, min_amount=100_000_000)

    assert [item["code"] for item in eligible] == ["600001"]
    assert "ST/*ST" in rejected["600002"][0]
    assert "停牌" in rejected["600003"][0]
    assert "上市不足" in rejected["600004"][0]
    assert "成交额" in rejected["600005"][0]


def test_missing_listing_date_fails_closed():
    record = _quote("600006", "上市日期未知", 2.0, 300_000_000)
    record.pop("listed_date")

    eligible, rejected = cp.filter_universe([record], min_amount=100_000_000)

    assert eligible == []
    assert any("上市日期缺失" in reason for reason in rejected["600006"])


def test_missing_kline_cannot_receive_strategy_score():
    ranked = cp.rank_candidates(
        [_quote("600001", "缺历史数据", 9.9, 2_000_000_000, turnover=20)],
        {},
    )

    assert ranked[0]["feature_ready"] is False
    assert ranked[0]["daban_score"] == 0
    assert ranked[0]["trend_score"] == 0


def test_rank_candidates_carries_atr_for_execution_plans():
    ranked = cp.rank_candidates(
        [_quote("600001", "测试股", 3.0, 500_000_000, turnover=8)],
        {"600001": _klines([10 + i * 0.1 for i in range(60)])},
    )

    assert ranked[0]["atr14"] is not None
    assert ranked[0]["atr14"] > 0


def test_rank_candidates_propagates_sector_from_live_context():
    ranked = cp.rank_candidates(
        [_quote("600001", "测试股", 3.0, 500_000_000, turnover=8)],
        {"600001": _klines([10 + i * 0.1 for i in range(60)])},
        signal_ctx={
            "lianban_ladder": {
                "600001": {"sector": "半导体", "lianban": 1},
            },
        },
    )

    assert ranked[0]["sector"] == "半导体"


def test_missing_kline_cannot_enter_watch_pool_as_balanced_fill():
    missing = _quote("600001", "缺历史数据", 9.9, 2_000_000_000, turnover=20)
    ready = _quote("600002", "历史完整", 2.0, 500_000_000, turnover=5)

    result = cp.build_watch_pool(
        [missing, ready],
        {"600002": _klines([10 + i * 0.05 for i in range(60)])},
        watch_limit=1,
    )

    assert [item["code"] for item in result["candidates"]] == ["600002"]


def test_dual_rankers_keep_daban_and_trend_separate():
    limit_up = _quote("600001", "打板股", 10.0, 900_000_000, turnover=18)
    trend = _quote("600002", "趋势股", 2.0, 1_200_000_000, turnover=4)
    noisy = _quote("600003", "噪声股", -2.0, 200_000_000, turnover=2)
    kline_by_code = {
        "600001": _klines([10.0] * 55 + [10.2, 10.5, 10.8, 11.0, 11.5]),
        "600002": _klines([8 + i * 0.08 for i in range(60)]),
        "600003": _klines([12 - i * 0.05 for i in range(60)]),
    }

    ranked = cp.rank_candidates([limit_up, trend, noisy], kline_by_code)
    by_code = {item["code"]: item for item in ranked}

    assert min(ranked, key=lambda item: item["daban_rank"])["code"] == "600001"
    assert min(ranked, key=lambda item: item["trend_rank"])["code"] == "600002"
    assert by_code["600001"]["daban_score"] > by_code["600002"]["daban_score"]
    assert by_code["600002"]["trend_score"] > by_code["600001"]["trend_score"]


def test_trend_ranker_rewards_lower_volatility_when_other_features_match():
    quotes = [
        _quote("600010", "平稳趋势", 2.0, 500_000_000),
        _quote("600011", "震荡趋势", 2.0, 500_000_000),
    ]
    smooth = [10 + i * 0.05 for i in range(60)]
    noisy = [
        value if i in {0, 39, 54, 59} else value + (0.04 if i % 2 else -0.04)
        for i, value in enumerate(smooth)
    ]

    ranked = cp.rank_candidates(
        quotes,
        {"600010": _klines(smooth), "600011": _klines(noisy)},
    )
    by_code = {item["code"]: item for item in ranked}

    assert by_code["600010"]["volatility_20d"] < by_code["600011"]["volatility_20d"]
    assert by_code["600010"]["trend_score"] > by_code["600011"]["trend_score"]


def test_build_watch_pool_scans_all_candidates_and_balances_strategies():
    quotes = [
        _quote(f"60{i:04d}", f"股票{i}", 9.8 if i % 7 == 0 else i % 5,
               150_000_000 + i * 1_000_000, turnover=3 + i % 10)
        for i in range(120)
    ]
    kline_by_code = {
        item["code"]: _klines([10 + day * (0.01 + idx / 100_000) for day in range(60)])
        for idx, item in enumerate(quotes)
    }

    result = cp.build_watch_pool(quotes, kline_by_code, watch_limit=40)

    assert result["eligible_count"] == 120
    assert result["scanned_count"] == 120
    assert len(result["candidates"]) == 40
    assert any(item["selected_by"]["daban"] for item in result["candidates"])
    assert any(item["selected_by"]["trend"] for item in result["candidates"])
    assert all(any(item["selected_by"].values()) for item in result["candidates"])


def test_missing_hot_money_state_closes_daban_but_keeps_trend_lane():
    quotes = [
        _quote("600001", "高分打板", 10.0, 1_000_000_000, turnover=18),
        _quote("600002", "趋势候选", 2.0, 900_000_000, turnover=5),
    ]
    klines = {
        item["code"]: _klines([10 + day * 0.05 for day in range(60)])
        for item in quotes
    }

    result = cp.build_watch_pool(
        quotes,
        klines,
        watch_limit=2,
        selection_state={"status": "insufficient_data", "daban_ready": False},
    )

    assert not any(item["selected_by"]["daban"] for item in result["candidates"])
    assert any(item["selected_by"]["trend"] for item in result["candidates"])


def test_non_mainline_candidate_cannot_consume_daban_quota():
    quotes = [
        {**_quote("600001", "非主线高分", 10.0, 2_000_000_000, turnover=20), "sector": "煤炭"},
        {**_quote("600002", "主线龙头", 9.8, 1_000_000_000, turnover=15), "sector": "半导体"},
    ]
    klines = {
        item["code"]: _klines([10 + day * 0.05 for day in range(60)])
        for item in quotes
    }
    selection_state = {
        "status": "ready",
        "daban_ready": True,
        "sectors": [
            {"sector": "半导体", "rank": 1, "qualified_for_daban": True},
            {"sector": "煤炭", "rank": 3, "qualified_for_daban": False},
        ],
        "stock_sectors": {"600001": "煤炭", "600002": "半导体"},
    }

    result = cp.build_watch_pool(
        quotes,
        klines,
        watch_limit=2,
        selection_state=selection_state,
    )
    by_code = {item["code"]: item for item in result["candidates"]}

    assert by_code["600001"]["selected_by"]["daban"] is False
    assert by_code["600002"]["selected_by"]["daban"] is True


def test_auction_shortlist_rejects_yiziban_and_limits_to_top_n():
    pool = {
        "asof": "2026-06-10",
        "candidates": [
            {
                "code": f"sh60{i:04d}",
                "name": f"股票{i}",
                "daban_score": 90 - i,
                "trend_score": 70 - i / 2,
            }
            for i in range(30)
        ],
    }
    factors = [
        {
            "code": f"sh60{i:04d}",
            "auction_gap_pct": 2.0,
            "auction_amount": 20_000_000 - i * 100_000,
            "auction_bid_ask_ratio": 2.5,
            "auction_net_bid_delta": 10_000,
            "is_yiziban": i == 0,
        }
        for i in range(30)
    ]

    result = cp.rank_auction_shortlist(pool, factors, limit=20)

    assert len(result["shortlist"]) == 20
    assert all(item["code"] != "sh600000" for item in result["shortlist"])
    rejected = {item["code"]: item for item in result["rejected"]}
    assert "一字板" in rejected["sh600000"]["rejection_reasons"][0]
    assert result["shortlist"][0]["auction_rank"] == 1


def test_auction_shortlist_preserves_daban_and_trend_lanes():
    pool = {
        "asof": "2026-06-10",
        "candidates": [
            {
                "code": f"sh600{i:03d}",
                "name": f"打板{i}",
                "daban_eligible": True,
                "daban_score": 95 - i,
                "trend_score": 10,
                "selected_by": {"daban": True, "trend": False},
            }
            for i in range(10)
        ] + [
            {
                "code": f"sz300{i:03d}",
                "name": f"趋势{i}",
                "daban_eligible": False,
                "daban_score": 0,
                "trend_score": 95 - i,
                "selected_by": {"daban": False, "trend": True},
            }
            for i in range(10)
        ],
    }
    factors = [
        {
            "code": item["code"],
            "auction_gap_pct": 2.0,
            "auction_amount": 20_000_000,
            "auction_bid_ask_ratio": 2.0,
            "auction_net_bid_delta": 10_000,
            "is_yiziban": False,
        }
        for item in pool["candidates"]
    ]

    result = cp.rank_auction_shortlist(pool, factors, limit=10)

    assert sum(item["auction_selected_by"]["daban"] for item in result["shortlist"]) >= 5
    assert sum(item["auction_selected_by"]["trend"] for item in result["shortlist"]) >= 5


def test_auction_fill_does_not_revive_non_mainline_daban_candidate():
    pool = {
        "asof": "2026-06-10",
        "candidates": [
            {
                "code": "sh600001",
                "name": "非主线高分",
                "daban_score": 99,
                "trend_score": 1,
                "hot_money_qualified": False,
                "selected_by": {"daban": True, "trend": False, "balanced_fill": False},
            },
            {
                "code": "sz300001",
                "name": "趋势候选",
                "daban_score": 0,
                "trend_score": 80,
                "hot_money_qualified": False,
                "selected_by": {"daban": False, "trend": True, "balanced_fill": False},
            },
        ],
    }
    factors = [
        {
            "code": item["code"],
            "auction_gap_pct": 2.0,
            "auction_amount": 20_000_000,
            "auction_bid_ask_ratio": 2.0,
            "auction_net_bid_delta": 10_000,
            "is_yiziban": False,
        }
        for item in pool["candidates"]
    ]

    result = cp.rank_auction_shortlist(pool, factors, limit=2)

    assert [item["code"] for item in result["shortlist"]] == ["sz300001"]


def test_auction_social_attention_is_current_bounded_tiebreaker():
    pool = {
        "asof": "2026-06-10",
        "candidates": [{
            "code": "sh600001",
            "name": "测试股",
            "daban_score": 80,
            "trend_score": 70,
        }],
    }
    factors = [{
        "code": "sh600001",
        "auction_gap_pct": 2.0,
        "auction_amount": 20_000_000,
        "auction_bid_ask_ratio": 2.0,
        "auction_net_bid_delta": 10_000,
        "is_yiziban": False,
    }]
    ctx = {
        "social_attention": {
            "stocks": {
                "600001": {
                    "attention_score": 95,
                    "attention_velocity": 80,
                    "cross_source_count": 2,
                    "eligible_for_boost": True,
                    "crowding_risk": "high",
                    "price_change_pct": 3,
                }
            }
        }
    }

    base = cp.rank_auction_shortlist(pool, factors, limit=1)["shortlist"][0]
    boosted = cp.rank_auction_shortlist(
        pool,
        factors,
        limit=1,
        signal_ctx=ctx,
    )["shortlist"][0]

    assert boosted["auction_social_attention_delta"] == 1.5
    assert boosted["auction_score"] - base["auction_score"] <= 1.5
    assert boosted["auction_score"] <= 100
