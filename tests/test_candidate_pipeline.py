"""Dynamic full-market candidate discovery and ranking tests."""

from datetime import date, timedelta

import candidate_pipeline as cp
import weak_market_delivery as wmd


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


def test_rank_candidates_keeps_coarse_industry_out_of_sector():
    ranked = cp.rank_candidates(
        [{**_quote("600001", "粗行业", 3.0, 500_000_000, turnover=8), "industry": "C 制造业"}],
        {"600001": _klines([10 + i * 0.1 for i in range(60)])},
    )

    assert ranked[0]["industry"] == "C 制造业"
    assert ranked[0]["sector"] is None


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


def test_weak_stale_market_blocks_broad_sector_trend_delivery():
    candidate = {
        **_quote("300001", "弱市趋势", 4.0, 1_000_000_000, turnover=12),
        "sector": "C 制造业",
    }
    result = cp.build_watch_pool(
        [candidate],
        {"300001": _klines([10 + day * 0.08 for day in range(60)])},
        watch_limit=1,
        selection_state={
            "status": "insufficient_data",
            "daban_ready": False,
            "market_timing": {
                "status": "insufficient_data",
                "breadth": {
                    "advancers": 756,
                    "decliners": 4394,
                    "flat": 55,
                    "limitup_count": 77,
                    "limitdown_count": 54,
                },
                "temperature": {"tier": "neutral", "context_fresh": False},
            },
            "sectors": [
                {"sector": "C 制造业", "rank": 1, "qualified_for_daban": False},
            ],
            "stock_sectors": {"300001": "C 制造业"},
        },
    )

    assert result["candidate_count"] == 0
    assert result["candidates"] == []
    evaluated = {item["code"]: item for item in result["evaluated_candidates"]}
    assert evaluated["300001"]["sector"] == "C 制造业"


def test_weak_market_delivery_requires_two_sector_evidence_types():
    quality = wmd.assess_delivery_quality(
        {
            **_quote("600001", "弱市龙头", 9.8, 1_000_000_000, turnover=15),
            "sector": "半导体",
            "sector_rank": 1,
            "leader_rank": 1,
            "hot_money_qualified": True,
            "sector_evidence_count": 1,
            "sector_evidence_types": ["limitup_cluster"],
        },
        lane="daban",
        stage="D0_close",
        selection_state={
            "market_timing": {
                "status": "ready",
                "breadth": {
                    "advancers": 900,
                    "decliners": 3900,
                    "flat": 100,
                    "limitup_count": 55,
                    "limitdown_count": 38,
                },
                "temperature": {"tier": "neutral", "context_fresh": True},
            }
        },
    )

    assert quality["status"] == "research_only"
    assert any("两类共振" in reason for reason in quality["reasons"])


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
            "auction_volume": 20_000 - i * 100,
            "prev_day_volume": 1_000_000,
            "matched": 2_000_000,
            "unmatched": 0,
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
            "auction_volume": 20_000,
            "prev_day_volume": 1_000_000,
            "matched": 2_000_000,
            "unmatched": 0,
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
            "auction_volume": 20_000,
            "prev_day_volume": 1_000_000,
            "matched": 2_000_000,
            "unmatched": 0,
            "auction_bid_ask_ratio": 2.0,
            "auction_net_bid_delta": 10_000,
            "is_yiziban": False,
        }
        for item in pool["candidates"]
    ]

    result = cp.rank_auction_shortlist(pool, factors, limit=2)

    assert [item["code"] for item in result["shortlist"]] == ["sz300001"]


def test_auction_shortlist_does_not_revive_weak_market_research_only_candidate():
    pool = {
        "asof": "2026-06-29",
        "candidates": [
            {
                "code": "sz300001",
                "name": "弱市趋势",
                "sector": "C 制造业",
                "daban_score": 0,
                "trend_score": 99,
                "selected_by": {"daban": False, "trend": True, "balanced_fill": False},
                "selection_context": {
                    "window": "D0_close",
                    "market_timing": {
                        "status": "insufficient_data",
                        "breadth": {
                            "advancers": 756,
                            "decliners": 4394,
                            "flat": 55,
                            "limitup_count": 77,
                            "limitdown_count": 54,
                        },
                        "temperature": {"tier": "neutral", "context_fresh": False},
                    },
                    "sector": {"name": "C 制造业", "rank": 1},
                    "leader": {"rank": 111},
                },
            },
        ],
    }
    factors = [
        {
            "code": "sz300001",
            "auction_gap_pct": 2.0,
            "auction_amount": 30_000_000,
            "auction_volume": 30_000,
            "prev_day_volume": 1_000_000,
            "matched": 3_000_000,
            "unmatched": 0,
            "auction_bid_ask_ratio": 2.0,
            "auction_net_bid_delta": 10_000,
            "is_yiziban": False,
        }
    ]

    result = cp.rank_auction_shortlist(pool, factors, limit=1)

    assert result["shortlist"] == []
    assert any("弱市" in reason for reason in result["rejected"][0]["rejection_reasons"])


def test_separate_research_pool_cannot_revive_into_execution_even_if_gate_reports_deliverable(
    monkeypatch,
):
    """Adversarial fence: pool membership, not a later score, owns execution admission."""
    monkeypatch.setattr(
        cp,
        "assess_delivery_quality",
        lambda *_args, **_kwargs: {"status": "deliverable_watch", "reasons": []},
    )
    research = {
        "code": "sh600001",
        "name": "研究高分票",
        "daban_score": 100,
        "trend_score": 100,
        "selected_by": {"daban": True, "trend": True},
        "research_only": True,
        "execution_action": "none",
    }
    pool = {
        "asof": "2026-08-20",
        "candidates": [],
        "research_candidates": [research],
        "execution_candidates": [],
        "gate": {"status": "weak_market", "reasons": ["弱市门禁"]},
    }
    factors = [{
        "code": research["code"],
        "auction_gap_pct": 2.0,
        "auction_amount": 50_000_000,
        "auction_volume": 50_000,
        "prev_day_volume": 1_000_000,
        "matched": 5_000_000,
        "unmatched": 0,
        "auction_bid_ask_ratio": 3.0,
        "auction_net_bid_delta": 20_000,
        "is_yiziban": False,
    }]

    result = cp.rank_auction_shortlist(pool, factors, limit=1)

    assert result["research_count"] == 1
    assert result["research_candidates"][0]["auction_score"] > 0
    assert result["execution_count"] == 0
    assert result["shortlist"] == []
    assert any(
        "仅保留研究" in reason
        for reason in result["rejected"][0]["rejection_reasons"]
    )


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
        "auction_volume": 20_000,
        "prev_day_volume": 1_000_000,
        "matched": 2_000_000,
        "unmatched": 0,
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


def test_ladder_stale_leader_qualified_false_still_delivers_trend_shortlist():
    """复现 07-06 P0：梯队缺档 → leader.qualified=False + hot_money_qualified=False，
    但正常市场竞价因子健康时，trend lane 短名单仍非空，daban lane 无人入选，
    且拒绝理由不含通用质量门槛 qualified=False。"""
    normal_timing = {
        "status": "ready",
        "breadth": {
            "advancers": 3100,
            "decliners": 1300,
            "flat": 90,
            "limitup_count": 88,
            "limitdown_count": 9,
        },
        "previous_ladder_premium": 3.2,
        "temperature": {"tier": "warm", "context_fresh": True},
    }
    pool = {
        "asof": "2026-07-06",
        "candidates": [
            {
                "code": f"sz300{i:03d}",
                "name": f"趋势{i}",
                "daban_score": 5,
                "trend_score": 95 - i,
                "hot_money_qualified": False,
                "selected_by": {"daban": False, "trend": True, "balanced_fill": False},
                "selection_context": {
                    "window": "D0_close",
                    "market_timing": normal_timing,
                    "sector": {"name": "半导体", "rank": 2},
                    "leader": {"rank": 20 + i, "qualified": False},
                },
            }
            for i in range(3)
        ],
    }
    factors = [
        {
            "code": item["code"],
            "auction_gap_pct": 2.0,
            "auction_amount": 30_000_000,
            "auction_volume": 30_000,
            "prev_day_volume": 1_000_000,
            "matched": 3_000_000,
            "unmatched": 0,
            "auction_bid_ask_ratio": 2.0,
            "auction_net_bid_delta": 10_000,
            "is_yiziban": False,
        }
        for item in pool["candidates"]
    ]

    result = cp.rank_auction_shortlist(pool, factors, limit=6)

    assert result["shortlist"], "trend lane 短名单不应为空"
    assert all(
        not item["auction_selected_by"]["daban"] for item in result["shortlist"]
    )
    for rejected in result.get("rejected", []):
        assert all(
            "候选质量门槛 qualified=False" not in reason
            for reason in rejected.get("rejection_reasons", [])
        )


def _auction_pool(*candidates):
    return {"asof": "2026-07-30", "candidates": list(candidates)}


def test_auction_shortlist_rejects_limit_down_candidate():
    """issue #139 贤丰控股：竞价跌停必须一票否决，不能靠前日涨停分放行。"""
    pool = _auction_pool({
        "code": "sz002141",
        "name": "贤丰控股",
        "daban_score": 96.26,
        "trend_score": 40,
    })
    factors = [{
        "code": "sz002141",
        "auction_gap_pct": -10.0,
        "auction_amount": 5_000_000,
        "auction_volume": 5_000,
        "prev_day_volume": 1_000_000,
        "matched": 500_000,
        "unmatched": 0,
        "auction_bid_ask_ratio": 0.01,
        "auction_net_bid_delta": 49_621,
        "board_status": "limit_down",
        "is_limit_down": True,
        "is_yiziban": False,
    }]

    result = cp.rank_auction_shortlist(pool, factors, limit=20)

    assert result["shortlist"] == []
    assert any("跌停" in reason for reason in result["rejected"][0]["rejection_reasons"])


def test_auction_shortlist_rejects_indicative_price_collapse():
    """issue #140 天融信：竞价从涨停价回落到平盘，回落 10% 必须一票否决。"""
    pool = _auction_pool({
        "code": "sz002212",
        "name": "天融信",
        "daban_score": 100.0,
        "trend_score": 60,
    })
    factors = [{
        "code": "sz002212",
        "auction_gap_pct": 0.0,
        "auction_max_gap_pct": 10.0,
        "auction_price_decay_pct": 10.0,
        "auction_faded_from_limit_up": True,
        "auction_amount": 0,
        "auction_volume": 0,
        "prev_day_volume": 1_000_000,
        "auction_bid_ask_ratio": 1.0,
        "auction_net_bid_delta": 0,
        "board_status": "flat_or_low_open",
        "is_yiziban": False,
    }]

    result = cp.rank_auction_shortlist(pool, factors, limit=20)

    assert result["shortlist"] == []
    assert any("回落" in reason for reason in result["rejected"][0]["rejection_reasons"])


def test_auction_shortlist_requires_matched_and_unmatched_contract():
    pool = _auction_pool({
        "code": "sh600519", "name": "量能契约", "daban_score": 90, "trend_score": 20,
    })
    common = {
        "code": "sh600519", "auction_gap_pct": 2.0, "auction_amount": 3000000,
        "auction_volume": 3000, "prev_day_volume": 1000000,
        "auction_bid_ask_ratio": 1.2, "auction_net_bid_delta": 100,
        "is_yiziban": False,
    }

    missing = cp.rank_auction_shortlist(pool, [common], limit=1)
    assert missing["shortlist"] == []
    assert any("matched" in reason for reason in missing["rejected"][0]["rejection_reasons"])

    valid = cp.rank_auction_shortlist(
        pool,
        [{**common, "matched": 300000, "unmatched": 0}],
        limit=1,
    )
    assert [item["code"] for item in valid["shortlist"]] == ["sh600519"]


def test_auction_shortlist_fail_closes_real_20260817_bad_factor_mix():
    """2026-08-17 回放：量能缺失和研究车道都不能进入可交易短名单。"""
    pool = _auction_pool(
        {
            "code": "sz002081",
            "name": "金螳螂",
            "daban_score": 98.79,
            "trend_score": 78.79,
            "selected_by": {"daban": True, "trend": False},
        },
        {
            "code": "sh600001",
            "name": "研究候选",
            "daban_score": 96.0,
            "trend_score": 95.0,
            "trend_live_weight": 0.0,
            "trend_lane_status": "research_only",
            "selected_by": {"daban": False, "trend": True},
        },
        {
            "code": "sh600002",
            "name": "量能完整候选",
            "daban_score": 60.0,
            "trend_score": 65.0,
            "daban_eligible": True,
            "hot_money_qualified": True,
            "selected_by": {"daban": True, "trend": False},
        },
    )
    factors = [
        {
            "code": "sz002081",
            "auction_gap_pct": -0.38,
            "auction_max_gap_pct": 10.02,
            "auction_price_decay_pct": 10.4,
            "auction_volume": 0.0,
            "prev_day_volume": None,
            "auction_amount": 0.0,
            "prev_day_amount": None,
            "auction_bid_ask_ratio": 0.97,
            "auction_net_bid_delta": 80662.0,
            "is_yiziban": False,
        },
        {
            "code": "sh600001",
            "auction_gap_pct": 2.0,
            "auction_volume": 30000.0,
            "prev_day_volume": 1000000.0,
            "matched": 3000000.0,
            "unmatched": 0.0,
            "auction_amount": 30000000.0,
            "prev_day_amount": 1000000000.0,
            "auction_bid_ask_ratio": 1.2,
            "auction_net_bid_delta": 10000.0,
            "is_yiziban": False,
        },
        {
            "code": "sh600002",
            "auction_gap_pct": 2.0,
            "auction_volume": 30000.0,
            "prev_day_volume": 1000000.0,
            "matched": 3000000.0,
            "unmatched": 0.0,
            "auction_amount": 30000000.0,
            "prev_day_amount": 1000000000.0,
            "auction_bid_ask_ratio": 2.0,
            "auction_net_bid_delta": 10000.0,
            "is_yiziban": False,
        },
    ]

    result = cp.rank_auction_shortlist(pool, factors, limit=2)

    shortlisted = {item["code"] for item in result["shortlist"]}
    assert "sz002081" not in shortlisted
    assert "sh600001" not in shortlisted
    assert "sh600002" in shortlisted
    rejected = {item["code"]: item for item in result["rejected"]}
    assert any("回落" in reason for reason in rejected["sz002081"]["rejection_reasons"])
    assert any("研究" in reason for reason in rejected["sh600001"]["rejection_reasons"])


def test_auction_prior_daban_alone_cannot_carry_a_weak_auction():
    """issue #140：前日涨停分权重降到 25%，平开 + 零量能 + 小幅回落必须被压到低分。"""
    pool = _auction_pool(
        {"code": "sz002212", "name": "弱竞价", "daban_score": 100.0, "trend_score": 100.0},
        {"code": "sh600001", "name": "健康竞价", "daban_score": 60.0, "trend_score": 60.0},
    )
    factors = [
        {
            "code": "sz002212",
            "auction_gap_pct": 0.0,
            "auction_max_gap_pct": 3.0,
            "auction_price_decay_pct": 3.0,
            "auction_amount": 0,
            "auction_volume": 1_000,
            "prev_day_volume": 1_000_000,
            "matched": 100_000,
            "unmatched": 0,
            "auction_bid_ask_ratio": 1.0,
            "auction_net_bid_delta": 0,
            "board_status": "flat_or_low_open",
            "is_yiziban": False,
        },
        {
            "code": "sh600001",
            "auction_gap_pct": 2.0,
            "auction_max_gap_pct": 2.0,
            "auction_price_decay_pct": 0.0,
            "auction_amount": 30_000_000,
            "auction_volume": 30_000,
            "prev_day_volume": 1_000_000,
            "matched": 3_000_000,
            "unmatched": 0,
            "auction_bid_ask_ratio": 2.0,
            "auction_net_bid_delta": 10_000,
            "board_status": "high_open",
            "is_yiziban": False,
        },
    ]

    by_code = {
        item["code"]: item
        for item in cp.rank_auction_shortlist(pool, factors, limit=20)["shortlist"]
    }

    assert by_code["sz002212"]["auction_score"] < 30
    assert by_code["sh600001"]["auction_score"] > by_code["sz002212"]["auction_score"]


def test_auction_zero_volume_fails_closed_instead_of_scoring():
    """竞价量能全为 0 时必须拒绝，不得靠横截面分位数产生可交易分。"""
    pool = _auction_pool(*[
        {"code": f"sh6000{i:02d}", "name": f"零量能{i}", "daban_score": 80, "trend_score": 80}
        for i in range(4)
    ])
    factors = [
        {
            "code": f"sh6000{i:02d}",
            "auction_gap_pct": 2.0,
            "auction_amount": 0,
            "auction_volume": 0,
            "prev_day_volume": None,
            "auction_bid_ask_ratio": 2.0,
            "auction_net_bid_delta": 10_000,
            "board_status": "high_open",
            "is_yiziban": False,
        }
        for i in range(4)
    ]

    result = cp.rank_auction_shortlist(pool, factors, limit=20)

    assert result["shortlist"] == []
    assert len(result["rejected"]) == 4
    assert all("量能关键字段" in reason for item in result["rejected"]
               for reason in item["rejection_reasons"])


def test_auction_degraded_book_halves_bid_and_delta_weight():
    """issue #140 P2：竞价数据降级时，委比/委买净增的权重减半，不得按可信数据计分。"""
    pool = _auction_pool(
        {"code": "sh600001", "name": "甲", "daban_score": 70, "trend_score": 70},
        {"code": "sh600002", "name": "乙", "daban_score": 70, "trend_score": 70},
    )

    def _factors(quality):
        return [
            {
                "code": code,
                "auction_gap_pct": 2.0,
                "auction_price_decay_pct": 0.0,
                "auction_amount": amount,
                    "auction_volume": max(1, int(amount / 1000)),
                    "prev_day_volume": 1_000_000,
                    "matched": max(100, int(amount / 10)),
                    "unmatched": 0,
                "auction_bid_ask_ratio": ratio,
                "auction_net_bid_delta": delta,
                "board_status": "high_open",
                "auction_data_quality": quality,
                "is_yiziban": False,
            }
            for code, amount, ratio, delta in (
                ("sh600001", 30_000_000, 2.0, 10_000),
                ("sh600002", 10_000_000, 1.0, 0),
            )
        ]

    def _top_score(quality):
        shortlist = cp.rank_auction_shortlist(pool, _factors(quality), limit=20)["shortlist"]
        return {item["code"]: item["auction_score"] for item in shortlist}["sh600001"]

    assert _top_score("degraded") < _top_score("ok")


def test_balanced_fill_does_not_revive_low_scoring_auction():
    """issue #140 P2：兜底通道设分数门槛，弱竞价票不得被 balanced_fill 捞回短名单。"""
    pool = _auction_pool({
        "code": "sz002212",
        "name": "弱竞价兜底",
        "daban_score": 40.0,
        "trend_score": 40.0,
        "selected_by": {"daban": False, "trend": False, "balanced_fill": True},
    })
    factors = [{
        "code": "sz002212",
        "auction_gap_pct": 0.5,
        "auction_price_decay_pct": 3.0,
        "auction_amount": 0,
        "auction_volume": 1_000,
        "prev_day_volume": 1_000_000,
        "matched": 100_000,
        "unmatched": 0,
        "auction_bid_ask_ratio": 1.0,
        "auction_net_bid_delta": 0,
        "board_status": "high_open",
        "auction_data_quality": "degraded",
        "is_yiziban": False,
    }]

    result = cp.rank_auction_shortlist(pool, factors, limit=20)

    assert result["shortlist"] == []
    assert any("兜底" in reason for reason in result["rejected"][0]["rejection_reasons"])


def test_rejected_records_each_candidate_once(monkeypatch):
    """同一只被拒候选不得在 rejected 里出现两次。

    兜底通道逐项判定时会记一次原因，末尾"未进入短名单"扫描又会记一次，
    于是被拒只数翻倍、且后写的通用原因盖过前面更具体的那条。下游
    （auction_collector 的拒绝清单、复盘里的原因分布）按条数统计即失真。
    """
    monkeypatch.setattr(
        cp,
        "assess_delivery_quality",
        lambda item, *, lane, stage, selection_state=None: {
            "status": "research_only",
            "reasons": ["弱市交付门禁未通过"],
        },
    )
    pool = {
        "asof": "2026-06-10",
        "candidates": [
            {
                "code": f"sh60{i:04d}",
                "name": f"股票{i}",
                "daban_score": 90 - i,
                "trend_score": 70 - i,
                "selected_by": {"daban": True, "trend": False},
            }
            for i in range(8)
        ],
    }
    factors = [
        {
            "code": f"sh60{i:04d}",
            "auction_gap_pct": 2.0,
            "auction_amount": 20_000_000 - i * 100_000,
            "auction_volume": 20_000 - i * 100,
            "prev_day_volume": 1_000_000,
            "matched": 2_000_000,
            "unmatched": 0,
            "auction_bid_ask_ratio": 2.5,
            "auction_net_bid_delta": 10_000,
            "is_yiziban": False,
        }
        for i in range(8)
    ]

    result = cp.rank_auction_shortlist(pool, factors, limit=5)

    # 没有一只通过交付门禁 → 短名单为空（行为不变）
    assert result["shortlist"] == []
    codes = [cp.naked_code(item["code"]) for item in result["rejected"]]
    assert len(codes) == len(set(codes)) == 8      # 8 只，各记一次
    # 保留的是具体门禁原因，而不是末尾扫描的通用兜底文案
    assert all(
        "弱市交付门禁未通过" in item["rejection_reasons"]
        for item in result["rejected"]
    )


def test_rejected_still_records_candidates_that_only_missed_the_top_n():
    """只是排名没进前 N（没被任何门禁拒过）的候选，仍必须出现在 rejected 里。

    去重不能顺手把这类唯一入账路径也去掉。
    """
    pool = {
        "asof": "2026-06-10",
        "candidates": [
            {
                "code": f"sh60{i:04d}",
                "name": f"股票{i}",
                "daban_score": 90 - i,
                "trend_score": 70 - i,
                "selected_by": {"daban": True, "trend": False},
            }
            for i in range(6)
        ],
    }
    factors = [
        {
            "code": f"sh60{i:04d}",
            "auction_gap_pct": 2.0,
            "auction_amount": 20_000_000 - i * 100_000,
            "auction_volume": 20_000 - i * 100,
            "prev_day_volume": 1_000_000,
            "matched": 2_000_000,
            "unmatched": 0,
            "auction_bid_ask_ratio": 2.5,
            "auction_net_bid_delta": 10_000,
            "is_yiziban": False,
        }
        for i in range(6)
    ]

    result = cp.rank_auction_shortlist(pool, factors, limit=2)

    assert len(result["shortlist"]) == 2
    shortlisted = {cp.naked_code(item["code"]) for item in result["shortlist"]}
    rejected_codes = [cp.naked_code(item["code"]) for item in result["rejected"]]
    # 落选的 4 只都在册且各只一次，与入选的互不重叠
    assert len(rejected_codes) == len(set(rejected_codes))
    assert set(rejected_codes) == {cp.naked_code(f"sh60{i:04d}") for i in range(6)} - shortlisted


# ---------------------------------------------------------------------------
# issue #260 B: 09:25 local_theme_candidates -> conditional_candidates
# ---------------------------------------------------------------------------


def _local_theme_member(code, sector, *, core=False):
    gate = {
        "schema": "local_theme_gate_v1",
        "sector": sector,
        "resonance_status": "observed",
        "execution_risk_status": "pending",
        "participation_scope": "local_theme_only",
        "confirmation_level": "preopen",
        "strong_member_count": 4,
        "observed_member_count": 6,
        "leader_isolated": False,
        "evidence_types": ["breadth", "limitup_cluster", "sector_flow"],
        "data_quality": "ok",
        "reason_codes": ["preopen_cannot_confirm"],
    }
    return {
        "code": code,
        "name": f"{sector}{code}",
        "sector": sector,
        "daban_score": 60,
        "trend_score": 30,
        "research_only": True,
        "execution_action": "none",
        "participation_scope": "local_theme_only",
        "admission_state": "local_observed",
        "local_theme_gate": gate,
    }


def _local_theme_factor(code, *, board_status, matched=2_000_000):
    return {
        "code": code,
        "auction_gap_pct": 9.9,
        "auction_amount": 20_000_000,
        "auction_volume": 20_000,
        "prev_day_volume": 1_000_000,
        "matched": matched,
        "unmatched": 0,
        "auction_bid_ask_ratio": 3.0,
        "auction_net_bid_delta": 5_000,
        "board_status": board_status,
        "is_yiziban": False,
        "is_limit_down": False,
    }


def test_local_theme_multi_member_confirms_into_conditional_candidates():
    members = [_local_theme_member(f"sh60{i:04d}", "贵金属") for i in range(4)]
    pool = {
        "asof": "2026-08-24",
        "research_candidates": members,
        "execution_candidates": [],
        "local_theme_candidates": members,
    }
    factors = [
        _local_theme_factor(m["code"], board_status="limit_up_with_ask") for m in members
    ]

    result = cp.rank_auction_shortlist(pool, factors, limit=20)

    assert result["conditional_count"] == 4
    codes = {cp.naked_code(item["code"]) for item in result["conditional_candidates"]}
    assert codes == {cp.naked_code(m["code"]) for m in members}
    for item in result["conditional_candidates"]:
        assert item["admission_state"] == "conditional_pending"
        assert item["local_theme_gate"]["resonance_status"] == "confirmed"
        assert item["local_theme_gate"]["execution_risk_status"] == "pending"
        assert item["research_only"] is True
        assert item["execution_action"] == "none"
    # 互斥：不得同时出现在可交易短名单/execution_candidates
    shortlist_codes = {cp.naked_code(item["code"]) for item in result["shortlist"]}
    assert not (codes & shortlist_codes)
    assert result["local_theme_count"] == 0


def test_local_theme_sector_stays_observed_when_auction_breadth_collapses():
    """9:25 只剩 1 只强势成员：龙头孤立/结构瓦解，不能升级为 conditional。"""
    members = [_local_theme_member(f"sh61{i:04d}", "电子") for i in range(4)]
    pool = {
        "asof": "2026-08-24",
        "research_candidates": members,
        "execution_candidates": [],
        "local_theme_candidates": members,
    }
    factors = [
        _local_theme_factor(members[0]["code"], board_status="limit_up_with_ask"),
        *(
            _local_theme_factor(m["code"], board_status="flat_or_low_open")
            for m in members[1:]
        ),
    ]

    result = cp.rank_auction_shortlist(pool, factors, limit=20)

    assert result["conditional_count"] == 0
    assert result["local_theme_count"] == 4
    for item in result["local_theme_candidates"]:
        assert item["local_theme_gate"]["resonance_status"] in ("observed", "none")
        assert item["admission_state"] == "local_observed"


def test_local_theme_degraded_auction_quality_blocks_conditional_confirmation():
    """镜像盘口/降级快照不得计入强势成员判定，确认结果必须是 blocked。"""
    members = [_local_theme_member(f"sh62{i:04d}", "通信") for i in range(4)]
    pool = {
        "asof": "2026-08-24",
        "research_candidates": members,
        "execution_candidates": [],
        "local_theme_candidates": members,
    }
    factors = []
    for member in members:
        factor = _local_theme_factor(member["code"], board_status="limit_up_with_ask")
        factor["auction_data_quality"] = {"status": "unavailable", "reasons": ["镜像盘口"]}
        factors.append(factor)

    result = cp.rank_auction_shortlist(pool, factors, limit=20)

    assert result["conditional_count"] == 0
    for item in result["local_theme_candidates"]:
        assert item["local_theme_gate"]["resonance_status"] == "blocked"


def test_ordinary_research_candidate_cannot_bypass_into_conditional_candidates():
    """普通研究票即使竞价评分很高，也不经过 local_theme 路径，不能获得局部准入。"""
    members = [_local_theme_member(f"sh63{i:04d}", "贵金属") for i in range(4)]
    plain_research = {
        "code": "sh640000",
        "name": "普通研究票",
        "sector": "贵金属",
        "daban_score": 99,
        "trend_score": 99,
        "research_only": True,
        "execution_action": "none",
    }
    pool = {
        "asof": "2026-08-24",
        "research_candidates": [*members, plain_research],
        "execution_candidates": [],
        "local_theme_candidates": members,
    }
    factors = [
        _local_theme_factor(m["code"], board_status="limit_up_with_ask") for m in members
    ] + [_local_theme_factor(plain_research["code"], board_status="limit_up_with_ask")]

    result = cp.rank_auction_shortlist(pool, factors, limit=20)

    conditional_codes = {cp.naked_code(item["code"]) for item in result["conditional_candidates"]}
    assert cp.naked_code(plain_research["code"]) not in conditional_codes
    assert conditional_codes == {cp.naked_code(m["code"]) for m in members}
