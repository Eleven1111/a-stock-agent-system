#!/usr/bin/env python3
"""
技术指标共享层（纯 list / 标准库，cron-safe，无第三方依赖）
==========================================================
统一 MA/EMA/MACD/RSI/KDJ 的纯 Python 实现，消除散落在 four_dim_scorer 与
chan_structure 的重复定义。实现与历史版本逐位一致（由 four_dim 指标测试守护），
切换 import 不改变任何数值。

口径说明：
- stock-analyst/tech_analysis.py 是 numpy/pandas 实现，技术栈不同、用途不同
  （worker 端深度分析），不在本模块收编范围，刻意保留。
- chanlun-backtest/fractal_chart.py 是展示型脚本，分型逻辑与 chan_structure 形态不同，
  暂留（后续可统一分型）。

用法:
    from indicators import calc_ma, calc_ema, calc_macd, calc_rsi, calc_kdj, macd_hist
"""

from typing import List, Optional, Tuple


def calc_ma(values: List[float], period: int) -> List[Optional[float]]:
    """简单移动平均（前 period-1 位为 None）。"""
    if len(values) < period:
        return [None] * len(values)
    result: List[Optional[float]] = [None] * (period - 1)
    for i in range(period - 1, len(values)):
        result.append(sum(values[i - period + 1:i + 1]) / period)
    return result


# 别名：部分脚本用 sma 命名
sma = calc_ma


def calc_ema(values: List[float], period: int) -> List[Optional[float]]:
    """指数移动平均。"""
    if len(values) < 2:
        return [None] * len(values)
    k = 2 / (period + 1)
    result: List[Optional[float]] = [values[0]]
    for i in range(1, len(values)):
        result.append(values[i] * k + result[-1] * (1 - k))
    return result


def calc_macd(closes: List[float], fast: int = 12, slow: int = 26,
              signal: int = 9) -> Tuple[List, List, List]:
    """MACD：返回 (DIF, DEA, 柱)。"""
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    dif = [f - s if f and s else None for f, s in zip(ema_fast, ema_slow)]
    dea = calc_ema([d for d in dif if d is not None], signal)
    dea_padded = [None] * (len(dif) - len(dea)) + dea
    macd_bars = [(d - de) * 2 if d is not None and de is not None else None
                 for d, de in zip(dif, dea_padded)]
    return dif, dea_padded, macd_bars


def macd_hist(closes: List[float], fast: int = 12, slow: int = 26,
              signal: int = 9) -> List[Optional[float]]:
    """便捷函数：只取 MACD 柱（chan_structure 背驰用）。"""
    return calc_macd(closes, fast, slow, signal)[2]


def calc_rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """RSI（Wilder 平滑）。"""
    if len(closes) < period + 1:
        return [None] * len(closes)
    result: List[Optional[float]] = [None] * period
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(diff if diff > 0 else 0)
        losses.append(-diff if diff < 0 else 0)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        if avg_loss == 0:
            result.append(100)
        else:
            rs = avg_gain / avg_loss
            result.append(100 - 100 / (1 + rs))
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    return result


def calc_kdj(highs: List[float], lows: List[float], closes: List[float],
             period: int = 9) -> Tuple[List, List, List]:
    """KDJ。"""
    n = len(closes)
    k_vals: List[Optional[float]] = [None] * n
    d_vals: List[Optional[float]] = [None] * n
    j_vals: List[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        hh = max(highs[i - period + 1:i + 1])
        ll = min(lows[i - period + 1:i + 1])
        rsv = (closes[i] - ll) / (hh - ll) * 100 if hh != ll else 50
        if k_vals[i - 1] is None:
            k_vals[i] = 50
            d_vals[i] = 50
        else:
            k_vals[i] = 2 / 3 * k_vals[i - 1] + 1 / 3 * rsv
            d_vals[i] = 2 / 3 * d_vals[i - 1] + 1 / 3 * k_vals[i]
        j_vals[i] = 3 * k_vals[i] - 2 * d_vals[i]
    return k_vals, d_vals, j_vals
