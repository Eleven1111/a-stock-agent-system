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


def test_stale_sector_flow_is_not_consumed_as_current_day_evidence():
    """Yesterday's failed-refresh value must not qualify today's sector."""
    quotes = [
        _quote("600001", "房地产", 4.0),
        _quote("600002", "房地产", 3.0),
        _quote("600003", "房地产", 2.0),
    ]
    context = {
        "sector_flows": {"房地产": -24.18},
        "sector_flows_asof": "2026-08-13",
        "sector_flows_stale": True,
    }
    timing = {
        "event_asof": "2026-08-14",
        "daban_ready": False,
        "breadth": {},
        "temperature": {},
    }

    state = hms.build_sector_leadership(
        quotes, context, timing, config=_config()
    )

    real_estate = next(row for row in state["sectors"] if row["sector"] == "房地产")
    assert real_estate["sector_flow_yi"] is None
    assert "sector_flow" not in real_estate["evidence_types"]


def test_stale_or_missing_context_fails_closed_for_daban():
    # 真过期 = 梯队整整跳过了一个交易日：事件日 06-23(周二) 的上一交易日是
    # 06-22(周一)，用 06-18(周四) 的梯队意味着 06-22 那场的梯队没拿到。
    timing = hms.build_market_timing(
        _quotes(),
        _context("2026-06-18"),
        event_asof="2026-06-23",
        config=_config(),
    )

    assert timing["status"] == "insufficient_data"
    assert timing["daban_ready"] is False
    assert any("过期" in reason or "不一致" in reason for reason in timing["reasons"])


def test_previous_trading_day_ladder_is_ready_across_weekend_and_holiday():
    """上一交易日收盘梯队用于当日盘前是合法的，允许量按交易日折算。

    事件日 06-22 是周一，上一交易日是 06-18(周四)——中间隔着周末**和**
    06-19 端午休市，自然日差 4 天。写死"允许 1 个自然日"会把这份合法梯队
    误判过期，让周一与节后首日的主线识别永远不生效。
    """
    assert hms.allowed_ladder_age_days("2026-06-22") == 4

    timing = hms.build_market_timing(
        _quotes(),
        _context("2026-06-18"),
        event_asof="2026-06-22",
        config=_config(),
    )

    assert timing["status"] == "ready"
    assert timing["daban_ready"] is True


def test_post_holiday_first_session_accepts_pre_holiday_ladder():
    # 国庆休市 10-01..10-07，节后首日 10-08 的上一交易日是 09-30（差 8 个自然日）。
    assert hms.allowed_ladder_age_days("2026-10-08") == 8

    timing = hms.build_market_timing(
        _quotes(),
        _context("2026-09-30"),
        event_asof="2026-10-08",
        config=_config(),
    )

    assert timing["daban_ready"] is True


def test_future_dated_ladder_still_fails_closed():
    # 放宽滞后允许量不得放过未来日期的梯队。
    timing = hms.build_market_timing(
        _quotes(),
        _context("2026-06-24"),
        event_asof="2026-06-23",
        config=_config(),
    )

    assert timing["daban_ready"] is False


def test_uncovered_calendar_year_fails_closed():
    # 交易日历未覆盖该年份 → 无法判定新鲜度 → fail-closed（AGENTS.md）。
    assert hms.allowed_ladder_age_days("2031-06-23") is None

    timing = hms.build_market_timing(
        _quotes(),
        _context("2031-06-22"),
        event_asof="2031-06-23",
        config=_config(),
    )

    assert timing["daban_ready"] is False
    assert any("日历未覆盖" in reason for reason in timing["reasons"])


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
    # 空间板高度 = 当日梯队最高连板数（600001 3 连板）
    assert timing["market_space_height"] == 3


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
        _context("2026-06-18"),
        event_asof="2026-06-23",
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


def test_weak_market_projects_social_attention_by_mainline_stock_membership():
    """Different provider taxonomies align by stock identity, not global aliases."""
    quotes = [
        _quote("600001", "医疗服务", 10.0, 1_500_000_000, 18),
        _quote("600002", "医疗服务", 9.9, 1_000_000_000, 14),
        _quote("600003", "医疗服务", 9.8, 800_000_000, 11),
        _quote("600004", "军工", -4.0),
        _quote("600005", "煤炭", -3.0),
        _quote("600006", "银行", -2.0),
        _quote("600007", "传媒", -5.0),
        _quote("600008", "有色", -6.0),
        _quote("600009", "通信", -7.0),
        _quote("600010", "食品", -8.0),
    ]
    context = {
        "ladder_asof": "2026-06-22",
        "lianban_ladder": {
            "600001": {"sector": "医疗服务", "lianban": 2},
            "600002": {"sector": "医疗服务", "lianban": 1},
            "600003": {"sector": "医疗服务", "lianban": 1},
        },
        "prev_lianban_ladder": {
            "600001": {"sector": "医疗服务", "lianban": 1},
            "600004": {"sector": "军工", "lianban": 1},
            "600005": {"sector": "煤炭", "lianban": 1},
            "600006": {"sector": "银行", "lianban": 1},
            "600007": {"sector": "传媒", "lianban": 1},
        },
        "sector_limitups": {"医疗服务": 3},
        "social_attention": {
            "schema": "social_attention_snapshot_v1",
            "themes": {
                "医疗器械": {"confirmed": True, "attention_score": 72.0},
                "医药生物": {"confirmed": True, "attention_score": 68.0},
            },
            "stocks": {
                "600001": {
                    "name": "股票600001",
                    "sector": "医疗器械",
                    "sector_source": "industry",
                    "industry": "医疗器械",
                    "industry_source": "industry_map",
                    "attention_score": 72.0,
                    "eligible_for_boost": True,
                },
                "600002": {
                    "name": "股票600002",
                    "sector": "医药生物",
                    "sector_source": "industry",
                    "industry": "医药生物",
                    "industry_source": "industry_map",
                    "attention_score": 68.0,
                    "eligible_for_boost": True,
                },
            },
        },
    }
    timing = hms.build_market_timing(
        quotes, context, event_asof="2026-06-22", config=_config()
    )

    state = hms.build_sector_leadership(quotes, context, timing, config=_config())
    medical = next(row for row in state["sectors"] if row["sector"] == "医疗服务")

    assert timing["weak_market"]["weak_regime"] is True
    assert medical["evidence_types"] == ["limitup_cluster", "social_theme"]
    assert medical["theme_confirmed"] is True
    assert medical["qualified_for_daban"] is True
    assert medical["theme_alignment"]["method"] == "stock_membership_projection"
    assert medical["theme_alignment"]["matched_stock_codes"] == ["600001", "600002"]
    assert medical["theme_alignment"]["source_sectors"] == [
        {
            "sector": "医疗器械",
            "sector_sources": ["industry"],
            "stock_count": 1,
            "matched_stock_codes": ["600001"],
        },
        {
            "sector": "医药生物",
            "sector_sources": ["industry"],
            "stock_count": 1,
            "matched_stock_codes": ["600002"],
        },
    ]


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

    # 身位落盘：候选自身连板数 + 当日空间板高度，供归因按板位切片（G4/G5）。
    assert by_code["600001"]["board_height"] == 3
    assert by_code["600002"]["board_height"] == 2
    assert by_code["600002"]["is_mid_position"] is True
    assert by_code["600003"]["board_height"] == 1
    assert by_code["600003"]["is_mid_position"] is False
    # 600007/600008 不在梯队里（无涨停），身位缺失诚实置 None，不冒充 0。
    assert by_code["600007"]["board_height"] is None
    assert by_code["600007"]["is_mid_position"] is False
    assert by_code["600001"]["market_space_height"] == 3


def test_leader_identity_carries_ladder_microstructure_into_reflexivity():
    context = _context()
    context["lianban_ladder"]["600001"]["first_seal"] = "09:30"
    timing = hms.build_market_timing(
        _quotes(), context, event_asof="2026-06-22", config=_config()
    )
    sectors = hms.build_sector_leadership(
        _quotes(), context, timing, config=_config()
    )
    candidates = [
        {**item, "daban_eligible": True, "hot_money_bonus": 10.0}
        for item in _quotes()
    ]

    ranked = hms.apply_leader_identity(candidates, sectors, context, config=_config())
    leader = next(item for item in ranked if item["code"] == "600001")

    assert leader["first_seal"] == "09:30"
    assert "open_burst_0925_0931" in leader["reflexivity"]["observed_facts"]


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


def test_selection_context_emits_hot_money_qualified_alongside_legacy_qualified():
    """selection_context_for 同时暴露 leader.hot_money_qualified 与旧 leader.qualified，
    且 sector.qualified_for_daban 与旧 sector.qualified 值一致。"""
    candidate = {
        "code": "600001",
        "sector": "半导体",
        "leader_rank": 1,
        "hot_money_qualified": True,
    }
    selection_state = {
        "status": "ready",
        "sectors": [
            {"sector": "半导体", "rank": 1, "qualified_for_daban": True},
        ],
    }

    context = hms.selection_context_for(candidate, selection_state, window="D0_close")

    leader = context["leader"]
    assert leader["hot_money_qualified"] is True
    assert leader["qualified"] == leader["hot_money_qualified"]
    sector = context["sector"]
    assert sector["qualified_for_daban"] is True
    assert sector["qualified"] == sector["qualified_for_daban"]


def test_selection_context_marks_non_qualified_candidate_on_both_keys():
    candidate = {"code": "600002", "sector": "银行", "hot_money_qualified": False}
    selection_state = {
        "status": "ready",
        "sectors": [{"sector": "银行", "rank": 5, "qualified_for_daban": False}],
    }

    context = hms.selection_context_for(candidate, selection_state, window="D0_close")

    assert context["leader"]["hot_money_qualified"] is False
    assert context["leader"]["qualified"] is False
    assert context["sector"]["qualified_for_daban"] is False
    assert context["sector"]["qualified"] is False


def test_compact_selection_context_carries_hot_money_qualified():
    context = hms.selection_context_for(
        {"code": "600001", "sector": "半导体", "hot_money_qualified": True},
        {
            "status": "ready",
            "sectors": [{"sector": "半导体", "rank": 1, "qualified_for_daban": True}],
        },
        window="D0_close",
    )

    compact = hms.compact_selection_context(context)

    assert compact["hot_money_qualified"] is True
    assert compact["qualified"] is True


def test_selection_context_carries_ladder_break_rate_and_board_height():
    """归因维度落盘（P0）：炸板率/涨跌比/空间板高度进 market_timing，
    候选自身身位进 leader，都是 signal_opened_event 的 selection_context 透传字段。"""
    candidate = {
        "code": "600001",
        "sector": "半导体",
        "leader_rank": 1,
        "hot_money_qualified": True,
        "board_height": 3,
        "is_mid_position": False,
    }
    selection_state = {
        "status": "ready",
        "sectors": [{"sector": "半导体", "rank": 1, "qualified_for_daban": True}],
        "market_timing": {
            "breadth": {"advancers": 60, "decliners": 20},
            "ladder_break_rate": 0.18,
            "market_space_height": 5,
        },
    }

    context = hms.selection_context_for(candidate, selection_state, window="D0_close")

    assert context["market_timing"]["ladder_break_rate"] == 0.18
    assert context["market_timing"]["advance_decline_ratio"] == 3.0
    assert context["market_timing"]["market_space_height"] == 5
    assert context["leader"]["board_height"] == 3
    assert context["leader"]["is_mid_position"] is False


def test_advance_decline_ratio_stays_none_without_decliners():
    """0 跌家不得算出恒真的正无穷涨跌比——按现有 fail-closed 纪律置 None。"""
    assert hms._advance_decline_ratio({"advancers": 10, "decliners": 0}) is None
    assert hms._advance_decline_ratio(None) is None


def test_advance_selection_context_carries_board_height_forward():
    d0_context = hms.selection_context_for(
        {"code": "600001", "sector": "半导体", "board_height": 2, "is_mid_position": True},
        {"status": "ready", "sectors": []},
        window="D0_close",
    )
    candidate = {
        "code": "600001",
        "selection_context": d0_context,
        "board_height": 2,
        "is_mid_position": True,
    }

    auction_context = hms.advance_selection_context(candidate, window="auction")

    assert auction_context["leader"]["board_height"] == 2
    assert auction_context["leader"]["is_mid_position"] is True
