"""LeaderScore 六因子 + ThemeScore 新·时·广（升级方案 P2，SHADOW ONLY）。

守五条：
1. 六因子按 config/scoring.yaml 的权重加权，不是模块内的影子副本；
2. "最高板 ≠ 龙头" —— 高位孤板 + 后排连续大面时 B/R **归零**（不是 unavailable）；
3. 深度池外 F/R 自动降级并**重新归一化**（不是当 0 分算，否则池外系统性低估）；
4. 全因子/全维度缺失返回 unavailable，绝不返回 0 分；
5. shadow 隔离：LeaderScore 取极值也不改动排序、身份标签与门禁结论。

所有 fixture 都是合成数据。本机无全市场日线缓存、sentiment_daily 覆盖率 1.15%，
本轮**不做**任何区分度/预测力断言 —— 那需要真实样本，见 docs 评估报告的 UNVERIFIED 清单。
"""

import pytest

import hot_money_selection as hms
import theme_strength as ts
from test_hot_money_selection import _config, _context, _quotes


# --- 配置来源 -----------------------------------------------------------

@pytest.fixture
def leader_cfg():
    """真实配置——不是测试自造的一份影子参数。"""
    loaded = hms.load_leader_score_config()
    assert loaded is not None, "config/scoring.yaml 缺 leader_score 节"
    return loaded


@pytest.fixture
def theme_cfg():
    loaded = ts.load_theme_score_config()
    assert loaded is not None, "config/scoring.yaml 缺 theme_score 节"
    return loaded


def _candidate(**overrides):
    base = {
        "code": "600001",
        "name": "样本一",
        "board_height": 3,
        "market_space_height": 5,
        "change_pct": 10.0,
        "first_seal": "09:40",
        "open_count": 0,
        "reseal_minutes": 0,
        "attention_score": 60.0,
    }
    base.update(overrides)
    return base


_SECTOR = {"sector": "半导体", "limitup_count": 5, "top10_change": 4.0}
_HEALTHY_HISTORY = [
    {"mid_board_break_rate": 0.1, "leader_damage": 3.0},
    {"mid_board_break_rate": 0.15, "leader_damage": 2.0},
]
_DAMAGED_HISTORY = [
    {"mid_board_break_rate": 0.7, "leader_damage": -8.0},
    {"mid_board_break_rate": 0.8, "leader_damage": -9.0},
]


def test_leader_score_config_is_the_only_source_of_weights(leader_cfg):
    weights = {
        name: float(spec["weight"]) for name, spec in leader_cfg["factors"].items()
    }
    assert weights == {
        "height": 0.25, "seal_speed": 0.20, "resilience": 0.15,
        "assist_breadth": 0.15, "relative_strength": 0.15, "attention": 0.10,
    }
    assert round(sum(weights.values()), 6) == 1.0


def test_leader_score_missing_config_is_unavailable_not_zero():
    result = hms.leader_score(_candidate(), sector_state=_SECTOR, config={})
    assert result["status"] == "unavailable"
    assert result["reason"] == "config_missing"
    assert result["score"] is None


# --- 六因子加权 ---------------------------------------------------------

def test_six_factors_weighted_sum_matches_manual_computation(leader_cfg):
    result = hms.leader_score(
        _candidate(), sector_state=_SECTOR, in_deep_pool=True,
        market_median_change=0.0, back_row_history=_HEALTHY_HISTORY, config=leader_cfg,
    )
    assert result["status"] == "ok"
    assert result["unavailable_factors"] == []
    assert result["available_weight"] == 1.0
    factors = result["factors"]
    # H = 3/5；F = 1 − 10min/60min；R = 开板 0 次 + 回封 0 分钟 = 1.0；
    # B = (5−1)/5；RS = 0.5 + (10−0)/20 与 0.5 + (10−4)/20 的均值；A = 60/100。
    assert factors["height"]["value"] == pytest.approx(0.6)
    assert factors["seal_speed"]["value"] == pytest.approx(1.0 - 10.0 / 60.0)
    assert factors["resilience"]["value"] == pytest.approx(1.0)
    assert factors["assist_breadth"]["value"] == pytest.approx(0.8)
    assert factors["relative_strength"]["value"] == pytest.approx((1.0 + 0.8) / 2)
    assert factors["attention"]["value"] == pytest.approx(0.6)
    manual = sum(row["value"] * row["weight"] for row in factors.values()) * 100.0
    assert result["score"] == pytest.approx(manual, abs=1e-3)


def test_first_seal_unparseable_is_unavailable_not_instant_seal(leader_cfg):
    result = hms.leader_score(
        _candidate(first_seal="盘中"), sector_state=_SECTOR, in_deep_pool=True,
        market_median_change=0.0, config=leader_cfg,
    )
    assert "seal_speed" in result["unavailable_factors"]
    assert "seal_speed" not in result["factors"]


# --- "最高板 ≠ 龙头" ----------------------------------------------------

def test_isolated_high_board_with_back_row_damage_zeroes_assist_and_resilience(leader_cfg):
    lonely = _candidate(board_height=6, market_space_height=6, first_seal="09:31")
    result = hms.leader_score(
        lonely, sector_state={"sector": "半导体", "limitup_count": 1, "top10_change": 4.0},
        in_deep_pool=True, market_median_change=0.0,
        back_row_history=_DAMAGED_HISTORY, config=leader_cfg,
    )
    assert result["isolation"]["isolated_high_board"] is True
    assert result["isolation"]["back_row_damage_days"] == 2
    # 归零：权重仍留在分母里（是观测到的 0，不是 unavailable）。
    assert result["factors"]["assist_breadth"]["value"] == 0.0
    assert result["factors"]["resilience"]["value"] == 0.0
    assert result["available_weight"] == 1.0
    assert "assist_breadth" not in result["unavailable_factors"]


def test_isolated_high_board_scores_below_supported_lower_board(leader_cfg):
    """最高板不等于龙头：孤板 + 后排大面的最高板，分数低于有助攻的低位板。"""
    lonely = hms.leader_score(
        _candidate(board_height=6, market_space_height=6, first_seal="09:31"),
        sector_state={"sector": "半导体", "limitup_count": 1, "top10_change": 4.0},
        in_deep_pool=True, market_median_change=0.0,
        back_row_history=_DAMAGED_HISTORY, config=leader_cfg,
    )
    supported = hms.leader_score(
        _candidate(code="600002", board_height=3, market_space_height=6),
        sector_state=_SECTOR, in_deep_pool=True, market_median_change=0.0,
        back_row_history=_HEALTHY_HISTORY, config=leader_cfg,
    )
    assert lonely["isolation"]["isolated_high_board"] is True
    assert supported["isolation"]["isolated_high_board"] is False
    assert lonely["score"] < supported["score"]


def test_highest_board_without_back_row_damage_keeps_factors(leader_cfg):
    """孤板但后排没有连续大面 → 不惩罚（三条件缺一不成立）。"""
    result = hms.leader_score(
        _candidate(board_height=6, market_space_height=6),
        sector_state={"sector": "半导体", "limitup_count": 1, "top10_change": 4.0},
        in_deep_pool=True, market_median_change=0.0,
        back_row_history=_HEALTHY_HISTORY, config=leader_cfg,
    )
    assert result["isolation"]["is_highest_board"] is True
    assert result["isolation"]["isolated_high_board"] is False
    assert result["factors"]["resilience"]["value"] == pytest.approx(1.0)


def test_back_row_damage_streak_undecidable_without_evidence(leader_cfg):
    penalty = leader_cfg["isolation_penalty"]
    assert hms.back_row_damage_streak(None, penalty) is None
    assert hms.back_row_damage_streak([], penalty) is None
    # 最近一日两个字段都缺 → 不可判定，不伪造惩罚也不伪造豁免。
    assert hms.back_row_damage_streak([{"mid_board_break_rate": 0.9}, {}], penalty) is None
    assert hms.back_row_damage_streak(_DAMAGED_HISTORY, penalty) == 2
    assert hms.back_row_damage_streak(
        [*_DAMAGED_HISTORY, {"mid_board_break_rate": 0.1, "leader_damage": 1.0}], penalty
    ) == 0


def test_isolation_not_triggered_when_damage_undecidable(leader_cfg):
    result = hms.leader_score(
        _candidate(board_height=6, market_space_height=6),
        sector_state={"sector": "半导体", "limitup_count": 1, "top10_change": 4.0},
        in_deep_pool=True, market_median_change=0.0,
        back_row_history=None, config=leader_cfg,
    )
    assert result["isolation"]["back_row_damage_days"] is None
    assert result["isolation"]["isolated_high_board"] is False


# --- 深度池外降级与重归一化 --------------------------------------------

def test_out_of_deep_pool_degrades_seal_and_resilience_and_renormalizes(leader_cfg):
    out = hms.leader_score(
        _candidate(), sector_state=_SECTOR, in_deep_pool=False,
        market_median_change=0.0, back_row_history=_HEALTHY_HISTORY, config=leader_cfg,
    )
    assert out["status"] == "ok"
    assert out["unavailable_factors"] == ["resilience", "seal_speed"]
    # 剩余四因子权重 0.25+0.15+0.15+0.10 = 0.65，重归一化后仍占满分母。
    assert out["available_weight"] == pytest.approx(0.65)
    manual = sum(row["value"] * row["weight"] for row in out["factors"].values())
    assert out["score"] == pytest.approx(manual / 0.65 * 100.0, abs=1e-3)


def test_out_of_pool_is_not_zero_imputation(leader_cfg):
    """池外 ≠ 把 F/R 当 0 分：重归一化的分数必须严格高于 0 分冒充的算法。"""
    out = hms.leader_score(
        _candidate(), sector_state=_SECTOR, in_deep_pool=False,
        market_median_change=0.0, back_row_history=_HEALTHY_HISTORY, config=leader_cfg,
    )
    zero_imputed = sum(
        row["value"] * row["weight"] for row in out["factors"].values()
    ) * 100.0  # 分母按 1.0 算 = 把缺失的 0.35 权重当 0 分
    assert out["score"] > zero_imputed
    assert out["score"] == pytest.approx(zero_imputed / 0.65, abs=1e-3)


def test_all_factors_missing_returns_unavailable_not_zero(leader_cfg):
    blank = {"code": "600999"}
    result = hms.leader_score(blank, sector_state={}, in_deep_pool=False, config=leader_cfg)
    assert result["status"] == "unavailable"
    assert result["reason"] == "insufficient_factor_weight"
    assert result["score"] is None
    assert result["available_weight"] == 0.0
    assert set(result["unavailable_factors"]) == set(leader_cfg["factors"])


def test_available_weight_below_floor_is_unavailable(leader_cfg):
    """只剩 H(0.25) + A(0.10) = 0.35 < min_available_weight(0.60) → 整体不可用。"""
    partial = {
        "code": "600003", "board_height": 2, "market_space_height": 4,
        "attention_score": 40.0,
    }
    result = hms.leader_score(partial, sector_state={}, in_deep_pool=False, config=leader_cfg)
    assert result["status"] == "unavailable"
    assert result["available_weight"] == pytest.approx(0.35)
    assert result["score"] is None


# --- shadow 隔离行为断言 -------------------------------------------------

def _ranked_fixture():
    timing = hms.build_market_timing(
        _quotes(), _context(), event_asof="2026-06-22", config=_config()
    )
    sectors = hms.build_sector_leadership(_quotes(), _context(), timing, config=_config())
    candidates = [
        {**item, "daban_eligible": True, "hot_money_bonus": 10.0} for item in _quotes()
    ]
    ranked = hms.apply_leader_identity(candidates, sectors, _context(), config=_config())
    return ranked, sectors


def test_shadow_scoring_leaves_ranking_identity_and_gate_untouched(leader_cfg):
    """行为断言：同一输入下，挂上 LeaderScore 前后每个候选逐字段一致、顺序一致。"""
    ranked, sectors = _ranked_fixture()
    scored = hms.apply_leader_score_shadow(
        ranked, sectors, deep_pool_codes=[item["code"] for item in ranked],
        market_median_change=0.0, back_row_history=_DAMAGED_HISTORY, config=leader_cfg,
    )
    assert [item["code"] for item in scored] == [item["code"] for item in ranked]
    for before, after in zip(ranked, scored):
        assert set(after) - set(before) == {"leader_score_shadow"}
        stripped = {k: v for k, v in after.items() if k != "leader_score_shadow"}
        assert stripped == before
        # 既有的排名代理分（100 − (rank−1)×15）与新分是两个字段，未被覆盖。
        assert after["leader_score"] == before["leader_score"]
        assert hms.selection_strategy_id(after, "daban") == hms.selection_strategy_id(
            before, "daban"
        )
        assert hms.selection_context_for(
            after, sectors, window="preopen"
        ) == hms.selection_context_for(before, sectors, window="preopen")


def test_extreme_shadow_score_does_not_change_gate_or_order(leader_cfg):
    """把 shadow 分推到极值（全池外 / 全孤板大面）也不改任何既有结论。"""
    ranked, sectors = _ranked_fixture()
    high = hms.apply_leader_score_shadow(
        ranked, sectors, deep_pool_codes=[item["code"] for item in ranked],
        market_median_change=-50.0, config=leader_cfg,
    )
    low = hms.apply_leader_score_shadow(
        ranked, sectors, deep_pool_codes=[],
        market_median_change=50.0, back_row_history=_DAMAGED_HISTORY, config=leader_cfg,
    )
    for baseline, hot, cold in zip(ranked, high, low):
        assert [hot["code"], cold["code"]] == [baseline["code"]] * 2
        assert hot["leader_rank"] == cold["leader_rank"] == baseline["leader_rank"]
        assert hot["leader_role"] == cold["leader_role"] == baseline["leader_role"]
        assert (
            hot["hot_money_qualified"]
            == cold["hot_money_qualified"]
            == baseline["hot_money_qualified"]
        )
        assert hot["hot_money_gate_reasons"] == baseline["hot_money_gate_reasons"]


def test_divergence_cases_are_collected_for_manual_review(leader_cfg):
    """LeaderScore 与现排序 Top2 不一致的案例落盘（方案 §5.2 第 1 条）。"""
    scored = [
        {"code": "600001", "sector": "半导体", "leader_rank": 1,
         "leader_score_shadow": {"status": "ok", "score": 40.0}},
        {"code": "600002", "sector": "半导体", "leader_rank": 2,
         "leader_score_shadow": {"status": "ok", "score": 30.0}},
        {"code": "600003", "sector": "半导体", "leader_rank": 3,
         "leader_score_shadow": {"status": "ok", "score": 90.0}},
    ]
    records = hms.leader_score_divergences(scored)
    assert len(records) == 1
    assert records[0]["current_top"] == ["600001", "600002"]
    assert records[0]["shadow_top"] == ["600001", "600003"]
    assert records[0]["only_in_shadow"] == ["600003"]


def test_no_divergence_when_shadow_agrees_or_is_unavailable():
    agreeing = [
        {"code": "600001", "sector": "半导体", "leader_rank": 1,
         "leader_score_shadow": {"status": "ok", "score": 90.0}},
        {"code": "600002", "sector": "半导体", "leader_rank": 2,
         "leader_score_shadow": {"status": "ok", "score": 80.0}},
        {"code": "600003", "sector": "半导体", "leader_rank": 3,
         "leader_score_shadow": {"status": "ok", "score": 10.0}},
    ]
    assert hms.leader_score_divergences(agreeing) == []
    # 缺分不是分歧：全部 unavailable 时不产出案例。
    blind = [{**item, "leader_score_shadow": {"status": "unavailable", "score": None}}
             for item in agreeing]
    assert hms.leader_score_divergences(blind) == []


# --- ThemeScore 新·时·广 ------------------------------------------------

_THEME = {"id": "th:ai-agent", "created_at": "2026-08-20", "members": ["600001"]}
_RECORD = {"breadth": {"status": "ok", "limit_up_count": 3, "up_ratio": 0.6}}


def test_theme_score_weights_come_from_config(theme_cfg):
    assert theme_cfg["weights"] == {"novelty": 0.35, "timing": 0.30, "breadth": 0.35}
    assert round(sum(theme_cfg["weights"].values()), 6) == 1.0


def test_theme_score_three_dimension_weighting(theme_cfg):
    result = ts.theme_score(
        _THEME, _RECORD, asof="2026-08-25",
        sentiment={"status": "ok", "band": "修复", "delta": 3.0},
        news_items=[{"title": "题材首发"}], seen_news_keys=[], config=theme_cfg,
    )
    assert result["status"] == "ok"
    assert result["available_weight"] == pytest.approx(1.0)
    dims = result["dimensions"]
    # N = 0.6×0.5^(5/5) + 0.4×1.0 = 0.7；T = 1.0(修复) + 0.1(ΔS>0) → clip 1.0；
    # B = 0.5×(3/5) + 0.5×0.6 = 0.6。
    assert dims["novelty"]["value"] == pytest.approx(0.7)
    assert dims["timing"]["value"] == pytest.approx(1.0)
    assert dims["breadth"]["value"] == pytest.approx(0.6)
    manual = (0.35 * 0.7 + 0.30 * 1.0 + 0.35 * 0.6) * 100.0
    assert result["score"] == pytest.approx(manual, abs=1e-3)


def test_theme_timing_degrades_and_renormalizes_when_sentiment_unavailable(theme_cfg):
    result = ts.theme_score(
        _THEME, _RECORD, asof="2026-08-25",
        sentiment={"status": "unavailable", "reason": "insufficient_history"},
        news_items=[{"title": "题材首发"}], seen_news_keys=[], config=theme_cfg,
    )
    assert result["status"] == "ok"
    assert result["unavailable_dimensions"] == ["timing"]
    assert result["available_weight"] == pytest.approx(0.70)
    manual = (0.35 * 0.7 + 0.35 * 0.6) / 0.70 * 100.0
    assert result["score"] == pytest.approx(manual, abs=1e-3)
    # 重归一化 ≠ 把 T 当 0 分。
    assert result["score"] > (0.35 * 0.7 + 0.35 * 0.6) * 100.0


def test_theme_timing_unmapped_band_is_unavailable(theme_cfg):
    timing = ts.compute_timing({"status": "ok", "band": "未知档"}, config=theme_cfg["timing"])
    assert timing["status"] == ts.UNAVAILABLE
    assert timing["reason"] == "band_not_mapped"


def test_theme_score_below_floor_is_unavailable_not_zero(theme_cfg):
    """只剩 breadth(0.35) < min_available_weight(0.65) → 整体不可用。"""
    result = ts.theme_score(
        {"id": "th:x"}, _RECORD, asof="2026-08-25", sentiment=None, config=theme_cfg
    )
    assert result["status"] == ts.UNAVAILABLE
    assert result["score"] is None
    assert result["available_weight"] == pytest.approx(0.35)
    assert result["unavailable_dimensions"] == ["novelty", "timing"]


def test_theme_score_all_dimensions_missing_is_unavailable(theme_cfg):
    result = ts.theme_score(
        {"id": "th:x"}, {"breadth": {"status": ts.UNAVAILABLE}},
        asof="2026-08-25", sentiment=None, config=theme_cfg,
    )
    assert result["status"] == ts.UNAVAILABLE
    assert result["available_weight"] == 0.0
    assert result["score"] is None


def test_theme_novelty_requires_explicit_seen_keys(theme_cfg):
    """seen_keys 未给出时新闻分不可用，绝不默认"全都是新的"。"""
    assert ts.news_novelty([{"title": "甲"}], seen_keys=None) is None
    assert ts.news_novelty(None, seen_keys=[]) is None
    assert ts.news_novelty([], seen_keys=[]) is None
    # 键的口径复用 novelty_gate.content_key：同题两条只算一条。
    assert ts.news_novelty([{"title": "甲"}, {"title": "甲"}], seen_keys=[]) == 1.0
    from novelty_gate import content_key
    seen = [content_key({"title": "甲"})]
    assert ts.news_novelty([{"title": "甲"}, {"title": "乙"}], seen_keys=seen) == 0.5


def test_theme_novelty_falls_back_to_age_only_and_renormalizes(theme_cfg):
    novelty = ts.compute_novelty(
        _THEME, asof="2026-08-25", news_items=None, seen_news_keys=None,
        config=theme_cfg["novelty"],
    )
    assert novelty["status"] == "ok"
    assert novelty["component_weight"] == pytest.approx(0.6)
    assert novelty["value"] == pytest.approx(0.5)  # 5 天 = 一个半衰期
    assert novelty["news_novelty"] is None


def test_theme_registry_age_unparseable_is_unavailable(theme_cfg):
    assert ts.registry_age_days("not-a-date", "2026-08-25") is None
    novelty = ts.compute_novelty(
        {"id": "th:x"}, asof="2026-08-25", config=theme_cfg["novelty"]
    )
    assert novelty["status"] == ts.UNAVAILABLE
    assert novelty["reason"] == "no_novelty_component"


def test_theme_score_missing_config_is_unavailable():
    result = ts.theme_score(_THEME, _RECORD, asof="2026-08-25", config={})
    assert result["status"] == ts.UNAVAILABLE
    assert result["reason"] == "config_missing"
    assert result["score"] is None


def test_existing_four_factor_sector_score_is_not_replaced():
    """ThemeScore 只是平行字段：既有 4 因子板块分与权重原样保留。"""
    assert hms.DEFAULT_CONFIG["sector_weights"] == {
        "limitup_count": 0.45, "amount": 0.20, "top10_change": 0.25, "attention": 0.10,
    }
    timing = hms.build_market_timing(
        _quotes(), _context(), event_asof="2026-06-22", config=_config()
    )
    sectors = hms.build_sector_leadership(_quotes(), _context(), timing, config=_config())
    for row in sectors["sectors"]:
        assert isinstance(row["score"], float)
        assert "theme_score" not in row
