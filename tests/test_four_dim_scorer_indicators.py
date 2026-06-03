"""四维打分引擎 — 技术指标边界条件测试"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'skills', 'stock-triage', 'scripts'))

from four_dim_scorer import calc_ma, calc_rsi, calc_macd, calc_kdj


def test_ma_empty():
    assert calc_ma([], 5) == []

def test_ma_insufficient():
    assert calc_ma([1.0, 2.0, 3.0], 5) == [None, None, None]

def test_ma_normal():
    prices = [10, 11, 12, 13, 14]
    result = calc_ma(prices, 3)
    assert result[0] is None
    assert result[1] is None
    assert abs(result[2] - 11.0) < 0.01
    assert abs(result[3] - 12.0) < 0.01
    assert abs(result[4] - 13.0) < 0.01

def test_ma_all_equal():
    prices = [10, 10, 10, 10, 10]
    result = calc_ma(prices, 3)
    assert all(x == 10.0 for x in result if x is not None)

def test_rsi_all_equal():
    # 全相等价格 → RSI 应不崩溃
    prices = [50.0] * 20
    result = calc_rsi(prices, 14)
    assert result[14] is not None

def test_rsi_uptrend():
    prices = [float(i) for i in range(20)]
    result = calc_rsi(prices, 14)
    assert result[-1] is not None

def test_macd_empty():
    dif, dea, hist = calc_macd([])
    assert dif == []

def test_macd_sufficient():
    prices = [float(i) for i in range(50)]
    dif, dea, hist = calc_macd(prices)
    assert len(dif) == 50
    assert dif[-1] is not None

def test_kdj_boundary():
    highs = [float(i+2) for i in range(20)]
    lows = [float(i) for i in range(20)]
    closes = [float(i+1) for i in range(20)]
    k, d, j = calc_kdj(highs, lows, closes, 9)
    assert k[-1] is not None
    assert d[-1] is not None
    assert j[-1] is not None
