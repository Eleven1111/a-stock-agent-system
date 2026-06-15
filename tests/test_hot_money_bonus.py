"""打板排名游资因子注入 — bonus 口径 / 无上下文零影响。"""

import candidate_pipeline as cp


def _ctx():
    return {
        "lianban_ladder": {
            "600001": {"lianban": 3, "sector": "半导体", "seal_yi": 2.0, "first_seal": "09:33"},
            "600002": {"lianban": 1, "sector": "封测", "first_seal": "13:10"},
        },
        "sector_limitups": {"半导体": 6, "封测": 2},
    }


def test_bonus_full_stack_leader():
    item = {"float_mktcap": 50e8}  # 50亿 → 封单比 2/50=4% ≥3%
    bonus, notes = cp.hot_money_bonus("600001", item, _ctx())
    # 8(3连板) + 4(首封09:33≤09:45) + 4(封单比理想) + 4(板块≥5) = 20 (cap)
    assert bonus == 20.0
    assert any("连板梯队" in n for n in notes)
    assert any("率先封板" in n for n in notes)
    assert any("赚钱效应" in n for n in notes)


def test_bonus_weak_follower():
    bonus, notes = cp.hot_money_bonus("600002", {}, _ctx())
    # 5(首板) + 0(首封13:10晚) + 0(无市值算不了封单比) + 0(板块2家<3) = 5
    assert bonus == 5.0


def test_bonus_not_in_ladder():
    bonus, notes = cp.hot_money_bonus("999999", {}, _ctx())
    assert bonus == 0.0 and notes == []


def test_bonus_none_ctx_zero():
    assert cp.hot_money_bonus("600001", {}, None) == (0.0, [])


def _bars(n=25, close=10.0):
    return [{"open": close, "close": close, "high": close + 0.1,
             "low": close - 0.1, "volume": 1000, "amount": 1e7} for _ in range(n)]


def test_rank_candidates_ctx_changes_daban_only():
    eligible = [
        {"code": "600001", "name": "甲", "amount": 5e8, "change_pct": 9.9, "turnover": 12.0},
        {"code": "600003", "name": "乙", "amount": 5e8, "change_pct": 9.9, "turnover": 12.0},
    ]
    klines = {"600001": _bars(), "600003": _bars()}
    base = {r["code"]: r for r in cp.rank_candidates(eligible, klines)}
    boosted = {r["code"]: r for r in cp.rank_candidates(eligible, klines, signal_ctx=_ctx())}
    # 在册票打板分提升，趋势分不变；无 ctx 行为与 Codex 原版一致
    assert boosted["600001"]["daban_score"] > base["600001"]["daban_score"]
    assert boosted["600001"]["hot_money_bonus"] > 0
    assert boosted["600003"]["daban_score"] == base["600003"]["daban_score"]
    assert boosted["600001"]["trend_score"] == base["600001"]["trend_score"]
    assert base["600001"]["hot_money_bonus"] == 0.0


def test_social_attention_is_a_bounded_discovery_overlay():
    eligible = [
        {
            "code": "600001",
            "name": "甲",
            "price": 10,
            "amount": 2e8,
            "turnover": 8,
            "change_pct": 5,
            "listed_date": "20200101",
        }
    ]
    klines = {"600001": _bars()}
    ctx = {
        "social_attention": {
            "schema": "social_attention_snapshot_v1",
            "stocks": {
                "600001": {
                    "attention_score": 95,
                    "attention_velocity": 80,
                    "cross_source_count": 2,
                    "eligible_for_boost": True,
                    "crowding_risk": "high",
                    "price_change_pct": 5,
                }
            },
        }
    }

    base = cp.rank_candidates(eligible, klines)[0]
    boosted = cp.rank_candidates(eligible, klines, signal_ctx=ctx)[0]

    assert 0 < boosted["social_attention_bonus"] <= 3
    assert boosted["daban_score"] - base["daban_score"] <= 3
    assert boosted["trend_score"] - base["trend_score"] <= 3
    assert boosted["social_attention"]["cross_source_count"] == 2
