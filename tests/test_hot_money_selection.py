"""Research-only timing, mainline-sector, and leader selection tests."""

import hot_money_selection as hms


def _quote(code, sector, change_pct, amount=500_000_000, turnover=8.0):
    return {
        "code": code,
        "name": f"股票{code}",
        "sector": sector,
        "price": 10.0 * (1 + change_pct / 100),
        "prev_close": 10.0,
        "change_pct": change_pct,
        "amount": amount,
        "turnover": turnover,
        "volume": 1_000_000,
    }


def _context(asof="2026-06-22"):
    return {
        "ladder_asof": asof,
        "lianban_ladder": {
            "600001": {"sector": "半导体", "lianban": 3},
            "600002": {"sector": "半导体", "lianban": 2},
            "600003": {"sector": "半导体", "lianban": 1},
            "600004": {"sector": "军工", "lianban": 1},
            "600005": {"sector": "军工", "lianban": 1},
            "600006": {"sector": "军工", "lianban": 1},
        },
        "prev_lianban_ladder": {
            "600001": {"sector": "半导体", "lianban": 2},
            "600004": {"sector": "军工", "lianban": 1},
        },
        "sector_limitups": {"半导体": 3, "军工": 3},
    }


def _quotes():
    return [
        _quote("600001", "半导体", 10.0, 1_500_000_000, 18),
        _quote("600002", "半导体", 9.9, 1_000_000_000, 14),
        _quote("600003", "半导体", 9.8, 800_000_000, 11),
        _quote("600004", "军工", 10.0, 700_000_000, 12),
        _quote("600005", "军工", 9.9, 600_000_000, 10),
        _quote("600006", "军工", 9.8, 500_000_000, 9),
        _quote("600007", "煤炭", 2.0, 400_000_000, 5),
        _quote("600008", "银行", -1.0, 900_000_000, 2),
    ]


def _config():
    return {
        "min_quote_count": 5,
        "min_sector_coverage": 0.5,
        "mainline_top_n": 2,
        "leader_top_n": 2,
        "min_sector_limitups": 3,
        "min_sector_evidence_types_weak": 2,
        "sector_flow_confirm_yi": 5.0,
        "sector_weights": {
            "limitup_count": 0.45,
            "amount": 0.20,
            "top10_change": 0.25,
            "attention": 0.10,
        },
    }


def test_build_sector_leadership_emits_crowding_fragility():
    timing = hms.build_market_timing(_quotes(), _context(), event_asof="2026-06-22", config=_config())
    state = hms.build_sector_leadership(_quotes(), _context(), timing, config=_config())
    # 市场级拥挤/脆弱字段在册（样本不足时 fails closed，不臆造分数）
    assert state["crowding_fragility"]["schema"] == "market_crowding_fragility_v1"
    # 板块级：每个 sector row 暴露 crowding/fragility 维度
    semi = next(r for r in state["sectors"] if r["sector"] == "半导体")
    assert "crowding_score" in semi and "fragility_score" in semi


def test_stale_or_missing_context_fails_closed_for_daban():
    timing = hms.build_market_timing(
        _quotes(),
        _context("2026-06-21"),
        event_asof="2026-06-22",
        config=_config(),
    )

    assert timing["status"] == "insufficient_data"
    assert timing["daban_ready"] is False
    assert any("过期" in reason or "不一致" in reason for reason in timing["reasons"])


def test_market_timing_derives_breadth_and_previous_ladder_premium():
    timing = hms.build_market_timing(
        _quotes(),
        _context(),
        event_asof="2026-06-22",
        config=_config(),
    )

    assert timing["status"] == "ready"
    assert timing["daban_ready"] is True
    assert timing["breadth"]["advancers"] == 7
    assert timing["breadth"]["decliners"] == 1
    assert timing["breadth"]["limitup_count"] == 6
    assert timing["previous_ladder_premium"] == 10.0


def test_market_timing_marks_stale_structural_weak_market():
    quotes = []
    for i in range(15):
        quotes.append(_quote(f"60{i:04d}", "C 制造业", 10.0))
    for i in range(15, 65):
        quotes.append(_quote(f"60{i:04d}", "C 制造业", -10.0))
    for i in range(65, 100):
        quotes.append(_quote(f"60{i:04d}", "C 制造业", -1.0))

    timing = hms.build_market_timing(
        quotes,
        _context("2026-06-21"),
        event_asof="2026-06-22",
        config=_config(),
    )

    assert timing["status"] == "insufficient_data"
    assert timing["weak_market"]["weak_regime"] is True
    assert timing["weak_market"]["extreme_weak"] is True
    assert timing["weak_market"]["status"] == "weak_data_stale"
    assert timing["weak_market"]["up_ratio"] == 0.15


def test_sector_leadership_is_cross_sectional_and_persistent():
    timing = hms.build_market_timing(
        _quotes(), _context(), event_asof="2026-06-22", config=_config()
    )
    previous = {
        "sectors": [
            {"sector": "半导体", "rank": 1, "qualified_for_daban": True},
            {"sector": "军工", "rank": 2, "qualified_for_daban": True},
        ]
    }

    state = hms.build_sector_leadership(
        _quotes(),
        _context(),
        timing,
        previous_snapshot=previous,
        config=_config(),
    )

    assert state["status"] == "ready"
    assert state["daban_ready"] is True
    assert [row["sector"] for row in state["sectors"][:2]] == ["半导体", "军工"]
    assert all(row["state"] == "confirmed" for row in state["sectors"][:2])
    assert all(row["qualified_for_daban"] for row in state["sectors"][:2])


def test_sector_leadership_consumes_normalized_social_stocks_and_themes():
    context = _context()
    context["social_attention"] = {
        "schema": "social_attention_snapshot_v1",
        "stocks": {
            "600001": {"sector": "半导体", "attention_score": 90},
        },
        "themes": {
            "半导体": {"attention_score": 88},
            "军工": {"attention_score": 20},
        },
    }
    timing = hms.build_market_timing(
        _quotes(), context, event_asof="2026-06-22", config=_config()
    )

    state = hms.build_sector_leadership(
        _quotes(), context, timing, config=_config()
    )
    sectors = {row["sector"]: row for row in state["sectors"]}

    assert sectors["半导体"]["attention"] == 88
    assert sectors["军工"]["attention"] == 20


def test_weak_market_mainline_requires_multi_evidence_confirmation():
    quotes = [
        _quote("600001", "半导体", 10.0, 1_500_000_000, 18),
        _quote("600002", "半导体", 9.9, 1_000_000_000, 14),
        _quote("600003", "半导体", 9.8, 800_000_000, 11),
        _quote("600004", "军工", -4.0),
        _quote("600005", "煤炭", -3.0),
        _quote("600006", "银行", -2.0),
        _quote("600007", "医药", -1.0),
        _quote("600008", "传媒", -5.0),
        _quote("600009", "有色", -6.0),
        _quote("600010", "通信", -7.0),
    ]
    context = {
        "ladder_asof": "2026-06-22",
        "lianban_ladder": {
            "600001": {"sector": "半导体", "lianban": 2},
            "600002": {"sector": "半导体", "lianban": 1},
            "600003": {"sector": "半导体", "lianban": 1},
        },
        "prev_lianban_ladder": {
            "600001": {"sector": "半导体", "lianban": 1},
            "600011": {"sector": "军工", "lianban": 1},
            "600012": {"sector": "传媒", "lianban": 1},
            "600013": {"sector": "有色", "lianban": 1},
            "600014": {"sector": "医药", "lianban": 1},
        },
        "sector_limitups": {"半导体": 3},
    }
    timing = hms.build_market_timing(
        quotes, context, event_asof="2026-06-22", config=_config()
    )

    weak_without_theme = hms.build_sector_leadership(
        quotes, context, timing, config=_config()
    )
    semi = next(row for row in weak_without_theme["sectors"] if row["sector"] == "半导体")
    assert timing["weak_market"]["weak_regime"] is True
    assert semi["evidence_types"] == ["limitup_cluster"]
    assert semi["qualified_for_daban"] is False
    assert weak_without_theme["status"] == "insufficient_data"

    context["social_attention"] = {
        "schema": "social_attention_snapshot_v1",
        "themes": {
            "半导体": {
                "attention_score": 88,
                "confirmed_stock_count": 1,
                "stock_count": 1,
                "confirmed": True,
            }
        },
        "stocks": {},
    }
    weak_with_theme = hms.build_sector_leadership(
        quotes, context, timing, config=_config()
    )
    semi = next(row for row in weak_with_theme["sectors"] if row["sector"] == "半导体")
    assert semi["evidence_types"] == ["limitup_cluster", "social_theme"]
    assert semi["evidence_count"] == 2
    assert semi["theme_confirmed"] is True
    assert semi["qualified_for_daban"] is True
    assert weak_with_theme["status"] == "ready"


def test_missing_sector_coverage_blocks_only_hot_money_lane():
    quotes = [{**item, "sector": ""} for item in _quotes()]
    context = _context()
    context["lianban_ladder"] = {}
    timing = hms.build_market_timing(
        quotes, context, event_asof="2026-06-22", config=_config()
    )
    state = hms.build_sector_leadership(
        quotes, context, timing, config=_config()
    )

    assert state["status"] == "insufficient_data"
    assert state["daban_ready"] is False
    assert state["sector_coverage"] == 0.0


def test_leader_identity_ranks_within_mainline_sector():
    timing = hms.build_market_timing(
        _quotes(), _context(), event_asof="2026-06-22", config=_config()
    )
    sectors = hms.build_sector_leadership(
        _quotes(), _context(), timing, config=_config()
    )
    candidates = [
        {**item, "daban_eligible": True, "hot_money_bonus": 10.0}
        for item in _quotes()
    ]

    ranked = hms.apply_leader_identity(
        candidates, sectors, _context(), config=_config()
    )
    by_code = {item["code"]: item for item in ranked}

    assert by_code["600001"]["sector_rank"] == 1
    assert by_code["600001"]["leader_rank"] == 1
    assert by_code["600001"]["leader_role"] == "sector_leader"
    assert by_code["600001"]["hot_money_qualified"] is True
    assert by_code["600003"]["hot_money_qualified"] is False
    assert by_code["600007"]["hot_money_qualified"] is False


def test_strategy_id_never_mislabels_generic_candidate_as_first_board_reseal():
    assert hms.selection_strategy_id({"hot_money_qualified": True}, "daban") == (
        "daban:mainline_leader_confirm"
    )
    assert hms.selection_strategy_id({}, "trend") == "trend_pullback"


def test_sector_leader_gets_structural_ablation():
    timing = hms.build_market_timing(_quotes(), _context(), event_asof="2026-06-22", config=_config())
    sectors = hms.build_sector_leadership(_quotes(), _context(), timing, config=_config())
    candidates = [{**item, "daban_eligible": True, "hot_money_bonus": 10.0} for item in _quotes()]
    ranked = hms.apply_leader_identity(candidates, sectors, _context(), config=_config())
    by_code = {item["code"]: item for item in ranked}

    # 半导体龙一(600001): 板块去龙头仍有涨停集群(600002/600003) 且不独占成交 → 结构性
    abl = by_code["600001"]["ablation"]
    assert abl["structural_leader"] is True
    assert abl["breadth_without_leader"] == 2
    assert 0 < abl["leader_amount_share"] < 0.6
    # 非龙头候选不做消融检验
    assert "ablation" not in by_code["600003"]


def test_leader_ablation_flags_isolated_single_core():
    # 板块仅龙头一个涨停 + 龙头独占成交 → 非结构性(孤立单核, 脆弱)
    leader = {"code": "600001", "name": "", "change_pct": 10.0, "amount": 9e8}
    abl = hms._leader_ablation(leader, {"limitup_count": 1, "amount": 1e9})
    assert abl["structural_leader"] is False
    assert abl["breadth_without_leader"] == 0
    assert abl["leader_amount_share"] == 0.9
