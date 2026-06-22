"""市场拥挤度/脆弱性度量（纯函数，不触网）。"""

import crowding_fragility as cf


def _q(code="600001", *, prev=10.0, open_=10.0, high=10.5, price=10.2,
       change=2.0, amount=1e8, name=""):
    return {
        "code": code, "name": name, "prev_close": prev, "open": open_,
        "high": high, "price": price, "change_pct": change, "amount": amount,
    }


CFG = {"min_market_observed": 3, "min_sector_observed": 3}


def test_market_fails_closed_when_coverage_thin():
    # 覆盖不足 → 不臆造高风险结论，scores 为 None，下游不被误杀
    r = cf.build_market_crowding_fragility([_q(), _q()], config={"min_market_observed": 5})
    assert r["status"] == "insufficient_data"
    assert r["crowding_score"] is None and r["fragility_score"] is None


def test_high_open_ratio_counts_gap_up_names():
    quotes = [
        _q(code="600001", open_=10.5),
        _q(code="600002", open_=10.3),
        _q(code="600003", open_=10.1),
        _q(code="600004", open_=9.8),
    ]
    r = cf.build_market_crowding_fragility(quotes, config=CFG)
    assert r["components"]["high_open_ratio"] == 0.75


def test_broke_board_ratio_distinguishes_sealed_from_failed():
    quotes = [
        _q(code="600001", high=11.0, price=11.0, change=10.0),  # 封住涨停
        _q(code="600002", high=11.0, price=10.5, change=5.0),   # 炸板
        _q(code="600003", high=11.0, price=10.4, change=4.0),   # 炸板
        _q(code="600004", high=10.3, price=10.2, change=2.0),   # 未触板
    ]
    r = cf.build_market_crowding_fragility(quotes, config=CFG)
    assert r["components"]["broke_board_ratio"] == round(2 / 3, 4)


def test_prev_strong_weakness_from_negative_premium():
    quotes = [_q(code=f"60000{i}") for i in range(4)]
    r = cf.build_market_crowding_fragility(
        quotes, market_timing={"previous_ladder_premium": -2.5}, config=CFG)
    assert r["components"]["prev_strong_weakness"] == 0.5
    assert r["previous_ladder_premium"] == -2.5


def test_limitdown_pressure_ratio():
    quotes = [
        _q(code="600001", change=10.0, price=11.0, high=11.0),
        _q(code="600002", change=-10.0, price=9.0),
        _q(code="600003", change=-10.0, price=9.0),
        _q(code="600004", change=1.0),
    ]
    r = cf.build_market_crowding_fragility(quotes, config=CFG)
    assert r["components"]["limitdown_pressure"] == 1.0


def test_amount_concentration_high_when_few_names_dominate():
    quotes = [_q(code="600001", amount=9e8)] + [
        _q(code=f"60010{i}", amount=1e6) for i in range(9)
    ]
    r = cf.build_market_crowding_fragility(quotes, config={**CFG, "top_concentration_n": 1})
    assert r["components"]["top_concentration"] > 0.95
    assert r["components"]["amount_hhi"] > 0.8


def test_top_concentration_none_when_sample_below_n():
    # 样本数 <= top_n 时不报集中度（避免"全部=1.0"的假高）
    quotes = [_q(code=f"6002{i:02d}", amount=1e8) for i in range(5)]
    r = cf.build_market_crowding_fragility(quotes, config={**CFG, "top_concentration_n": 20})
    assert r["components"]["top_concentration"] is None


def test_calm_market_scores_low():
    quotes = [
        _q(code=f"6002{i:02d}", open_=10.0, high=10.2, price=10.1, change=1.0)
        for i in range(10)
    ]
    r = cf.build_market_crowding_fragility(quotes, config=CFG)
    assert 0.0 <= r["crowding_score"] < 0.3
    assert 0.0 <= r["fragility_score"] < 0.3


def test_signals_fire_on_crowded_fragile_market():
    quotes = [
        _q(code=f"6001{i:02d}", open_=10.6, high=11.0, price=10.5, change=5.0)
        for i in range(8)
    ]
    r = cf.build_market_crowding_fragility(quotes, config=CFG)
    assert any("拥挤" in s or "炸板" in s or "脆弱" in s for s in r["signals"])


def test_weighted_renormalizes_over_available_parts():
    parts = {"a": 1.0, "b": None, "c": 0.0}
    weights = {"a": 0.5, "b": 0.3, "c": 0.2}
    assert cf._weighted(parts, weights) == round(0.5 / 0.7, 4)


def test_weighted_none_when_all_parts_missing():
    assert cf._weighted({"a": None}, {"a": 1.0}) is None


def test_sector_level_uses_low_threshold():
    members = [
        _q(code="600001", open_=10.5),
        _q(code="600002", open_=10.4),
        _q(code="600003", open_=9.9),
    ]
    r = cf.sector_crowding_fragility(members)
    assert r["status"] == "ready"
    assert r["observed"] == 3
    assert r["crowding_score"] is not None


def test_sector_insufficient_below_threshold():
    r = cf.sector_crowding_fragility([_q(), _q()])
    assert r["status"] == "insufficient_data"
    assert r["crowding_score"] is None
