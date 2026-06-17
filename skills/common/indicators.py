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


def calc_atr(highs: List[float], lows: List[float], closes: List[float],
             period: int = 14) -> List[Optional[float]]:
    """Average True Range（Wilder 平滑）。"""
    n = len(closes)
    if n < 2:
        return [None] * n
    trs: List[float] = [highs[0] - lows[0]]
    for i in range(1, n):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    result: List[Optional[float]] = [None] * (period - 1)
    if len(trs) < period:
        return [None] * n
    atr = sum(trs[:period]) / period
    result.append(atr)
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
        result.append(atr)
    return result


def calc_volume_ratio(volumes: List[float], current_idx: int = -1,
                      window: int = 5) -> Optional[float]:
    """量比 = 当前成交量 / 过去 N 日同期均量。"""
    if current_idx < 0:
        current_idx = len(volumes) + current_idx
    if current_idx < window or current_idx >= len(volumes):
        return None
    avg = sum(volumes[current_idx - window:current_idx]) / window
    return round(volumes[current_idx] / avg, 2) if avg > 0 else None


def calc_chip_concentration(closes: List[float], volumes: List[float],
                            period: int = 60, pct: float = 0.9) -> Optional[float]:
    """筹码集中度：最近 period 日加权成本分布中 pct 获利区间宽度(%)。

    简化实现：用成交量加权平均成本 ± 标准差覆盖 90% 区间，
    区间越窄说明筹码越集中（主力控盘/建仓完成）。
    """
    n = min(period, len(closes))
    if n < 10 or len(volumes) < n:
        return None
    c = closes[-n:]
    v = volumes[-n:]
    total_v = sum(v)
    if total_v <= 0:
        return None
    avg_cost = sum(p * vol for p, vol in zip(c, v)) / total_v
    variance = sum(vol * (p - avg_cost) ** 2 for p, vol in zip(c, v)) / total_v
    std = variance ** 0.5
    if avg_cost <= 0:
        return None
    return round(2 * std / avg_cost * 100, 2)


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
