"""common/indicators.py — 统一技术指标的数值守护。"""

import indicators as ind


def test_calc_ma_basic():
    assert ind.calc_ma([1, 2, 3, 4, 5], 5)[-1] == 3.0
    assert ind.calc_ma([1, 2], 5) == [None, None]


def test_sma_is_alias_of_ma():
    assert ind.sma is ind.calc_ma


def test_calc_ema_seed_is_first_value():
    assert ind.calc_ema([10, 11, 12], 2)[0] == 10


def test_calc_macd_triple_lengths():
    closes = [float(i) for i in range(1, 40)]
    dif, dea, hist = ind.calc_macd(closes)
    assert len(dif) == len(dea) == len(hist) == 39


def test_macd_hist_matches_calc_macd():
    closes = [float(i % 5) for i in range(40)]
    assert ind.macd_hist(closes) == ind.calc_macd(closes)[2]


def test_calc_rsi_all_up_is_100():
    closes = [float(i) for i in range(1, 20)]  # 严格递增
    assert ind.calc_rsi(closes, 14)[-1] == 100


def test_calc_kdj_runs():
    n = 20
    highs = [10 + (i % 3) for i in range(n)]
    lows = [9 - (i % 2) for i in range(n)]
    closes = [9.5 for _ in range(n)]
    k, d, j = ind.calc_kdj(highs, lows, closes, 9)
    assert k[-1] is not None and d[-1] is not None and j[-1] is not None
