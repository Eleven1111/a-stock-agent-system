"""
技术指标计算（纯 numpy，无 talib 依赖）
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple

def ema(arr: np.ndarray, period: int) -> np.ndarray:
    """指数移动平均"""
    result = np.zeros_like(arr, dtype=float)
    result[:] = np.nan
    if len(arr) < period:
        return result
    # SMA 作为起始值
    result[period-1] = np.mean(arr[:period])
    multiplier = 2.0 / (period + 1)
    for i in range(period, len(arr)):
        result[i] = (arr[i] - result[i-1]) * multiplier + result[i-1]
    return result

def sma(arr: np.ndarray, period: int) -> np.ndarray:
    """简单移动平均"""
    result = np.zeros_like(arr, dtype=float)
    result[:] = np.nan
    for i in range(period - 1, len(arr)):
        result[i] = np.mean(arr[i - period + 1:i + 1])
    return result

def compute_macd(close: np.ndarray, fast=12, slow=26, signal=9) -> Dict:
    """MACD指标"""
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    dif = ema_fast - ema_slow
    dea = ema(dif, signal)
    macd_hist = 2 * (dif - dea)
    return {"DIF": dif, "DEA": dea, "MACD": macd_hist}

def compute_rsi(close: np.ndarray, period=14) -> np.ndarray:
    """RSI指标"""
    result = np.zeros_like(close, dtype=float)
    result[:] = np.nan

    if len(close) <= period:
        return result

    deltas = np.diff(close)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        result[period] = 100
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - (100 / (1 + rs))

    for i in range(period + 1, len(close)):
        avg_gain = (avg_gain * (period - 1) + gains[i-1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i-1]) / period
        if avg_loss == 0:
            result[i] = 100
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - (100 / (1 + rs))

    return result

def compute_kdj(high: np.ndarray, low: np.ndarray, close: np.ndarray, n=9, m1=3, m2=3) -> Dict:
    """KDJ指标"""
    result_k = np.zeros_like(close, dtype=float)
    result_d = np.zeros_like(close, dtype=float)
    result_j = np.zeros_like(close, dtype=float)
    result_k[:] = np.nan
    result_d[:] = np.nan
    result_j[:] = np.nan

    if len(close) < n:
        return {"K": result_k, "D": result_d, "J": result_j}

    for i in range(n - 1, len(close)):
        hh = np.max(high[i - n + 1:i + 1])
        ll = np.min(low[i - n + 1:i + 1])
        if hh == ll:
            rsv = 50
        else:
            rsv = (close[i] - ll) / (hh - ll) * 100

        if i == n - 1:
            result_k[i] = rsv
            result_d[i] = rsv
        else:
            result_k[i] = (2 * result_k[i-1] + rsv) / 3
            result_d[i] = (2 * result_d[i-1] + result_k[i]) / 3

        result_j[i] = 3 * result_k[i] - 2 * result_d[i]

    return {"K": result_k, "D": result_d, "J": result_j}

def compute_boll(close: np.ndarray, period=20, ndev=2) -> Dict:
    """布林带"""
    ma = sma(close, period)
    std = np.zeros_like(close, dtype=float)
    std[:] = np.nan

    for i in range(period - 1, len(close)):
        std[i] = np.std(close[i - period + 1:i + 1])

    upper = ma + ndev * std
    lower = ma - ndev * std

    return {"MA": ma, "UPPER": upper, "LOWER": lower, "STD": std}


# ─── 综合分析 ───

def analyze_stock(code: str, name: str = "", kline_data: Optional[List[Dict]] = None,
                  realtime: Optional[Dict] = None) -> Dict:
    """个股综合分析"""
    from . import data_cache

    # 获取K线
    if kline_data is None:
        kline_data = data_cache.fetch_kline(code, 180)

    if not kline_data or len(kline_data) < 20:
        return {"code": code, "name": name, "error": "数据不足", "signals": {}, "rating": "N/A"}

    df = pd.DataFrame(kline_data)
    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float) if 'high' in df.columns else close
    low = df['low'].values.astype(float) if 'low' in df.columns else close
    volume = df['volume'].values.astype(float) if 'volume' in df.columns else np.zeros_like(close)

    # 计算指标
    macd = compute_macd(close)
    rsi_6 = compute_rsi(close, 6)
    rsi_14 = compute_rsi(close, 14)
    kdj = compute_kdj(high, low, close)
    boll = compute_boll(close)
    ma5 = sma(close, 5)
    ma10 = sma(close, 10)
    ma20 = sma(close, 20)
    ma60 = sma(close, 60)
    vol_ma5 = sma(volume, 5)
    vol_ma10 = sma(volume, 10)

    # 当前值
    i = len(close) - 1
    cur_close = close[i]
    cur_price = realtime['price'] if realtime and realtime.get('price') else cur_close
    cur_pct = realtime['pct_change'] if realtime and realtime.get('pct_change') else 0

    # 信号判断
    signals = {}

    # 1. 趋势信号
    trend_signal = 0
    if not np.isnan(ma5[i]) and not np.isnan(ma20[i]):
        if ma5[i] > ma20[i] and ma5[i-1] <= ma20[i-1]:
            signals['ma_golden_cross'] = "MA5上穿MA20（金叉）🟢"
            trend_signal += 2
        elif ma5[i] < ma20[i] and ma5[i-1] >= ma20[i-1]:
            signals['ma_death_cross'] = "MA5下穿MA20（死叉）🔴"
            trend_signal -= 2

        # 均线排列
        if not np.isnan(ma60[i]):
            if ma5[i] > ma10[i] > ma20[i] > ma60[i]:
                signals['ma_arrangement'] = "多头排列 📈"
                trend_signal += 1
            elif ma5[i] < ma10[i] < ma20[i] < ma60[i]:
                signals['ma_arrangement'] = "空头排列 📉"
                trend_signal -= 1

    # 2. MACD信号
    if not np.isnan(macd['DIF'][i]) and not np.isnan(macd['DEA'][i]):
        if macd['DIF'][i] > macd['DEA'][i] and macd['DIF'][i-1] <= macd['DEA'][i-1]:
            signals['macd_golden'] = "MACD金叉 🟢"
            trend_signal += 2
        elif macd['DIF'][i] < macd['DEA'][i] and macd['DIF'][i-1] >= macd['DEA'][i-1]:
            signals['macd_death'] = "MACD死叉 🔴"
            trend_signal -= 2
        elif macd['DIF'][i] > macd['DEA'][i]:
            signals['macd_status'] = "MACD多头 🟢"
            trend_signal += 1
        else:
            signals['macd_status'] = "MACD空头 🔴"
            trend_signal -= 1

    # 3. RSI信号
    if not np.isnan(rsi_14[i]):
        if rsi_14[i] < 30:
            signals['rsi'] = f"RSI({rsi_14[i]:.1f}) 超卖区 💡"
            trend_signal += 1
        elif rsi_14[i] > 70:
            signals['rsi'] = f"RSI({rsi_14[i]:.1f}) 超买区 ⚠️"
            trend_signal -= 1
        elif 40 <= rsi_14[i] <= 60:
            signals['rsi'] = f"RSI({rsi_14[i]:.1f}) 中性区"
        else:
            signals['rsi'] = f"RSI({rsi_14[i]:.1f})"

    # 4. KDJ信号
    if not np.isnan(kdj['K'][i]) and not np.isnan(kdj['D'][i]):
        if kdj['K'][i] > kdj['D'][i] and kdj['K'][i-1] <= kdj['D'][i-1]:
            signals['kdj'] = f"KDJ金叉(K{kdj['K'][i]:.1f}>D{kdj['D'][i]:.1f}) 🟢"
            trend_signal += 1
        elif kdj['K'][i] < kdj['D'][i] and kdj['K'][i-1] >= kdj['D'][i-1]:
            signals['kdj'] = f"KDJ死叉(K{kdj['K'][i]:.1f}<D{kdj['D'][i]:.1f}) 🔴"
            trend_signal -= 1
        elif kdj['K'][i] > 80:
            signals['kdj'] = f"KDJ超买(K{kdj['K'][i]:.1f}) ⚠️"
            trend_signal -= 0.5
        elif kdj['K'][i] < 20:
            signals['kdj'] = f"KDJ超卖(K{kdj['K'][i]:.1f}) 💡"
            trend_signal += 0.5

    # 5. 布林带位置
    if not np.isnan(boll['UPPER'][i]) and not np.isnan(boll['LOWER'][i]):
        if cur_close > boll['UPPER'][i] * 1.01:
            signals['boll'] = f"突破布林上轨({cur_close:.2f}>{boll['UPPER'][i]:.2f}) ⚠️⚠️"
            trend_signal -= 2
        elif cur_close >= boll['UPPER'][i]:
            signals['boll'] = f"触及布林上轨({boll['UPPER'][i]:.2f}) ⚠️"
            trend_signal -= 1
        elif cur_close < boll['LOWER'][i] * 0.99:
            signals['boll'] = f"跌破布林下轨({cur_close:.2f}<{boll['LOWER'][i]:.2f}) 💡💡"
            trend_signal += 2
        elif cur_close <= boll['LOWER'][i]:
            signals['boll'] = f"触及布林下轨({boll['LOWER'][i]:.2f}) 💡"
            trend_signal += 1
        else:
            mid_pct = (cur_close - boll['LOWER'][i]) / (boll['UPPER'][i] - boll['LOWER'][i]) * 100
            signals['boll'] = f"布林中偏{mid_pct:.0f}%位"

    # 6. 成交量分析
    if vol_ma5[i] > 0 and i >= 1:
        vol_ratio = volume[i] / vol_ma5[i]
        if vol_ratio > 2:
            signals['volume'] = f"放量{vol_ratio:.1f}倍 🔥"
            trend_signal += 1 if cur_pct > 0 else -1
        elif vol_ratio < 0.5:
            signals['volume'] = f"缩量({vol_ratio:.1f}x)"

    # 综合评分 (-10 到 +10)
    signals['score'] = round(trend_signal, 1)

    # ⚠️ 客观性硬约束
    # 1. 趋势空头时，评分上限锁定
    if not np.isnan(ma5[i]) and not np.isnan(ma10[i]) and not np.isnan(ma20[i]) and not np.isnan(ma60[i]):
        if ma5[i] < ma10[i] < ma20[i] < ma60[i]:  # 完全空头
            trend_signal = min(trend_signal, -0.5)  # 最多给观望偏空
        elif cur_close < ma5[i] and cur_close < ma20[i]:  # 价格在均线下方
            # 即使超卖，最多给中性
            trend_signal = min(trend_signal, 1.0)

    # 2. 跌破布林下轨+趋势向下 → 不自动视为买入信号（可能是加速下跌）
    if not np.isnan(boll['UPPER'][i]) and not np.isnan(boll['LOWER'][i]):
        if cur_close < boll['LOWER'][i]:
            if not np.isnan(ma20[i]) and cur_close < ma20[i]:
                # 跌破布林下轨+价格在MA20下方=加速下跌，不是抄底信号
                if 'boll' in signals and '💡' in signals['boll']:
                    signals['boll'] = signals['boll'].replace('💡💡', '⚠️ 加速下跌中，等待企稳')
                    # 不反向加分

    # 评级
    if trend_signal >= 4:
        rating = "强烈买入 🟢🟢"
    elif trend_signal >= 2:
        rating = "买入 🟢"
    elif trend_signal >= 0:
        rating = "观望 🌤"
    elif trend_signal >= -2:
        rating = "谨慎 🔶"
    else:
        rating = "卖出/回避 🔴"

    # 支撑/阻力位
    support = None
    resistance = None
    if not np.isnan(boll['LOWER'][i]):
        support = round(boll['LOWER'][i], 2)
    if not np.isnan(boll['UPPER'][i]):
        resistance = round(boll['UPPER'][i], 2)

    # 近5日涨跌幅
    pct_5d = None
    pct_10d = None
    if len(close) >= 6:
        pct_5d = (close[i] - close[i-5]) / close[i-5] * 100
    if len(close) >= 11:
        pct_10d = (close[i] - close[i-10]) / close[i-10] * 100

    return {
        "code": code,
        "name": name or code,
        "price": round(cur_price, 2),
        "pct_change": round(cur_pct, 2),
        "pct_5d": round(pct_5d, 2) if pct_5d else None,
        "pct_10d": round(pct_10d, 2) if pct_10d else None,
        "rating": rating,
        "score": trend_signal,
        "signals": signals,
        "support": support,
        "resistance": resistance,
        "ma5": round(ma5[i], 2) if not np.isnan(ma5[i]) else None,
        "ma10": round(ma10[i], 2) if not np.isnan(ma10[i]) else None,
        "ma20": round(ma20[i], 2) if not np.isnan(ma20[i]) else None,
        "ma60": round(ma60[i], 2) if i < len(ma60) and not np.isnan(ma60[i]) else None,
        "data_points": len(kline_data),
    }


def screen_stocks(codes_with_names: List[Tuple[str, str]], use_realtime=True) -> List[Dict]:
    """批量分析多只股票"""
    from . import data_cache

    realtime_data = {}
    if use_realtime:
        codes = [c for c, _ in codes_with_names]
        realtime_data = data_cache.fetch_realtime(codes)

    results = []
    for code, name in codes_with_names:
        rt = realtime_data.get(code, {})
        result = analyze_stock(code, name, realtime=rt)
        results.append(result)

    # 按评分排序
    results.sort(key=lambda x: x.get('score', 0), reverse=True)
    return results


def format_report(results: List[Dict]) -> str:
    """格式化输出分析报告"""
    lines = []
    lines.append(f"{'代码':<8} {'名称':<10} {'现价':<8} {'涨跌':<8} {'评分':<6} {'评级':<16} {'支撑':<8} {'阻力':<8} {'信号':<30}")
    lines.append("─" * 100)

    for r in results:
        if 'error' in r:
            lines.append(f"{r['code']:<8} {r['name']:<10} {'数据不足':<30}")
            continue

        # 收集信号摘要
        sigs = []
        for k, v in r.get('signals', {}).items():
            if k != 'score' and isinstance(v, str):
                sigs.append(v.split('(')[0][:12])
        sig_str = " | ".join(sigs[:3])

        lines.append(
            f"{r['code']:<8} {r['name']:<10} "
            f"{r['price']:<8.2f} {r['pct_change']:>+6.2f}% "
            f"{r['score']:<+6} {r['rating']:<16} "
            f"{str(r['support'] or '-'):<8} {str(r['resistance'] or '-'):<8} "
            f"{sig_str:<30}"
        )

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from . import data_cache

    cmd = sys.argv[1] if len(sys.argv) > 1 else "analyze"

    if cmd == "analyze":
        code = sys.argv[2] if len(sys.argv) > 2 else "600519"
        name = sys.argv[3] if len(sys.argv) > 3 else ""

        rt = data_cache.fetch_realtime([code])
        result = analyze_stock(code, name, realtime=rt.get(code))

        print(f"\n{'='*60}")
        print(f" {result['name']}({result['code']}) 技术分析")
        print(f"{'='*60}")
        print(f" 现价: {result['price']:.2f} | 今日: {result['pct_change']:+.2f}%")
        if result.get('pct_5d'):
            print(f" 近5日: {result['pct_5d']:+.2f}% | 近10日: {result['pct_10d']:+.2f}%")
        print(f"\n 评级: {result['rating']} (综合分: {result['score']:+d})")
        print("\n 关键位置:")
        if result.get('ma5'): print(f"   MA5: {result['ma5']}  MA10: {result['ma10']}  MA20: {result['ma20']}")
        if result.get('ma60'): print(f"   MA60(趋势线): {result['ma60']}")
        if result.get('support'): print(f"   布林下轨(支撑): {result['support']}")
        if result.get('resistance'): print(f"   布林上轨(阻力): {result['resistance']}")
        print("\n 技术信号:")
        for k, v in result['signals'].items():
            if k != 'score':
                print(f"   {v}")
        print(f" 数据: {result['data_points']}个交易日")

    elif cmd == "screen":
        pairs = [(sys.argv[i], sys.argv[i+1]) for i in range(2, len(sys.argv), 2)]
        if not pairs:
            pairs = [("600519","贵州茅台"),("000858","五粮液")]
        results = screen_stocks(pairs)
        print(format_report(results))
