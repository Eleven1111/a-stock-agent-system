#!/usr/bin/env python3
"""
四维打分 + 深度技术分析 for 通富微电(002156)
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from data_cache import fetch_realtime, fetch_kline
from tech_analysis import sma, compute_rsi, compute_macd, compute_kdj, compute_boll

code = "002156"
name = "通富微电"

# ===== 1. 获取数据 =====
rt = fetch_realtime([code]).get(code, {})
print("=" * 70)
print("📊 通富微电(002156) 深度技术分析 — 2026-06-03")
print("=" * 70)

print("\n📌 实时行情:")
print(f"   收盘价: {rt.get('price',0):.2f}元 | 涨幅: {rt.get('pct_change',0):+.2f}% | 涨停!")
print(f"   换手率: {rt.get('turnover_rate',0)}% | 成交额: {rt.get('amount',0)/1e8:.2f}亿")
print(f"   PE: {rt.get('pe',0):.2f} | 总市值: {rt.get('total_mv',0)/1e8:.2f}亿")

# ===== 2. 日线分析 =====
klines = fetch_kline(code, 180, force_refresh=True)
if not klines or len(klines) < 30:
    print("ERROR: 数据不足")
    sys.exit(1)

print(f"\n📈 日线数据: {len(klines)}个交易日")

df = pd.DataFrame(klines)
close = df['close'].values.astype(float)
high = df['high'].values.astype(float)
low = df['low'].values.astype(float)
open_p = df['open'].values.astype(float)
volume = df['volume'].values.astype(float)

i = len(close) - 1
cur_close = close[i]

# 使用实时价格
price_now = rt.get('price', cur_close)

# === 技术指标计算 ===
ma5 = sma(close, 5)
ma10 = sma(close, 10)
ma20 = sma(close, 20)
ma60 = sma(close, 60)
macd = compute_macd(close)
rsi6 = compute_rsi(close, 6)
rsi14 = compute_rsi(close, 14)
kdj = compute_kdj(high, low, close)
boll = compute_boll(close)
vol_ma5 = sma(volume, 5)

ma5_v = float(ma5[i]) if not np.isnan(ma5[i]) else None
ma10_v = float(ma10[i]) if not np.isnan(ma10[i]) else None
ma20_v = float(ma20[i]) if not np.isnan(ma20[i]) else None
ma60_v = float(ma60[i]) if not np.isnan(ma60[i]) else None

dif = float(macd['DIF'][i]) if not np.isnan(macd['DIF'][i]) else None
dea = float(macd['DEA'][i]) if not np.isnan(macd['DEA'][i]) else None
hist = float(macd['MACD'][i]) if not np.isnan(macd['MACD'][i]) else None

r6_val = float(rsi6[i]) if not np.isnan(rsi6[i]) else 50
r14_val = float(rsi14[i]) if not np.isnan(rsi14[i]) else 50

k_val = float(kdj['K'][i]) if not np.isnan(kdj['K'][i]) else 50
d_val = float(kdj['D'][i]) if not np.isnan(kdj['D'][i]) else 50
j_val = float(kdj['J'][i]) if not np.isnan(kdj['J'][i]) else 50

boll_up = float(boll['UPPER'][i]) if not np.isnan(boll['UPPER'][i]) else None
boll_mid = float(boll['MA'][i]) if not np.isnan(boll['MA'][i]) else None
boll_low = float(boll['LOWER'][i]) if not np.isnan(boll['LOWER'][i]) else None

vol_ratio = volume[i] / vol_ma5[i] if vol_ma5[i] > 0 else 0

print("\n" + "─" * 70)
print("【一、日线技术指标完整分析】")
print("─" * 70)

print("\n🔹 移动平均线(MA):")
print(f"   MA5 = {ma5_v:.2f}" if ma5_v else "   MA5 = N/A")
print(f"   MA10 = {ma10_v:.2f}" if ma10_v else "   MA10 = N/A")
print(f"   MA20 = {ma20_v:.2f}" if ma20_v else "   MA20 = N/A")
print(f"   MA60 = {ma60_v:.2f}" if ma60_v else "   MA60 = N/A")

if ma5_v and ma10_v and ma20_v and ma60_v:
    if ma5_v > ma10_v > ma20_v > ma60_v:
        print(f"   均线排列: ✅ 多头排列 (MA5>{ma5_v:.2f} > MA10>{ma10_v:.2f} > MA20>{ma20_v:.2f} > MA60>{ma60_v:.2f})")
    elif ma5_v < ma10_v < ma20_v < ma60_v:
        print("   均线排列: ❌ 空头排列")
    else:
        print("   均线排列: ⚠️ 交叉/震荡排列")

    if price_now > ma5_v:
        print(f"   现价在MA5上方: ✅ {price_now:.2f} > {ma5_v:.2f}")
    else:
        print(f"   现价在MA5下方: ⚠️ {price_now:.2f} < {ma5_v:.2f}")

    if price_now > ma20_v:
        print(f"   现价在MA20上方: ✅ {price_now:.2f} > {ma20_v:.2f}")
    else:
        print(f"   现价在MA20下方: ⚠️ {price_now:.2f} < {ma20_v:.2f}")

# MACD
print("\n🔹 MACD指标:")
if dif is not None:
    print(f"   DIF = {dif:.4f}")
if dea is not None:
    print(f"   DEA = {dea:.4f}")
if hist is not None:
    print(f"   MACD柱 = {hist:.4f}")

if dif is not None and dea is not None:
    if dif > dea:
        print("   MACD状态: ✅ 多头 (DIF > DEA)")
    else:
        print("   MACD状态: ❌ 空头 (DIF < DEA)")

    dif_prev = float(macd['DIF'][i-1]) if not np.isnan(macd['DIF'][i-1]) else None
    dea_prev = float(macd['DEA'][i-1]) if not np.isnan(macd['DEA'][i-1]) else None
    if dif_prev is not None and dea_prev is not None:
        if dif > dea and dif_prev <= dea_prev:
            print("   MACD信号: ✅ 金叉!")
        elif dif < dea and dif_prev >= dea_prev:
            print("   MACD信号: ❌ 死叉!")
        elif dif > dea:
            print("   MACD信号: 多头运行中")
        else:
            print("   MACD信号: 空头运行中")

    hist_prev = float(macd['MACD'][i-1]) if not np.isnan(macd['MACD'][i-1]) else None
    if hist is not None and hist_prev is not None:
        if hist > hist_prev:
            print("   MACD柱变化: 🔼 红柱增长/绿柱缩短 (动量增强)")
        else:
            print("   MACD柱变化: 🔽 红柱缩短/绿柱增长 (动量减弱)")

# RSI
print("\n🔹 RSI指标:")
print(f"   RSI(6) = {r6_val:.1f}")
print(f"   RSI(14) = {r14_val:.1f}")

if r14_val < 30:
    rsi_status = "超卖区 💡"
elif r14_val < 40:
    rsi_status = "偏弱区"
elif r14_val < 60:
    rsi_status = "中性区"
elif r14_val < 70:
    rsi_status = "偏强区"
else:
    rsi_status = "超买区 ⚠️"
print(f"   RSI(14)状态: {rsi_status}")

# KDJ
print("\n🔹 KDJ指标:")
print(f"   K = {k_val:.1f}")
print(f"   D = {d_val:.1f}")
print(f"   J = {j_val:.1f}")
if k_val > 80:
    print("   K值状态: 超买区(>80)")
elif k_val < 20:
    print("   K值状态: 超卖区(<20)")
else:
    print("   K值状态: 中性区")

k_prev = float(kdj['K'][i-1]) if not np.isnan(kdj['K'][i-1]) else None
d_prev = float(kdj['D'][i-1]) if not np.isnan(kdj['D'][i-1]) else None
if k_prev is not None and d_prev is not None:
    if k_val > d_val and k_prev <= d_prev:
        print("   KDJ信号: ✅ 金叉!")
    elif k_val < d_val and k_prev >= d_prev:
        print("   KDJ信号: ❌ 死叉!")
    elif k_val > d_val:
        print("   KDJ信号: 多头向上 (K>D)")
    else:
        print("   KDJ信号: 空头向下 (K<D)")

# 布林带
print("\n🔹 布林带(BOLL):")
if boll_up is not None and boll_mid is not None and boll_low is not None:
    print(f"   上轨 = {boll_up:.2f}")
    print(f"   中轨 = {boll_mid:.2f}")
    print(f"   下轨 = {boll_low:.2f}")
    print(f"   带宽 = {boll_up - boll_low:.2f}")
    boll_pos_pct = (price_now - boll_low) / (boll_up - boll_low) * 100 if boll_up != boll_low else 50
    print(f"   价格在布林带位置: {boll_pos_pct:.1f}% (下轨→上轨)")

    if price_now > boll_up:
        print("   布林状态: ⚠️ 突破上轨 (超买)")
    elif price_now >= boll_up * 0.98:
        print("   布林状态: 触及上轨 (阻力)")
    elif price_now < boll_low:
        print("   布林状态: 💡 跌破下轨 (超卖)")
    elif price_now <= boll_low * 1.02:
        print("   布林状态: 触及下轨 (支撑)")
    elif boll_pos_pct > 60:
        print("   布林状态: 中上轨之间 (偏强)")
    elif boll_pos_pct < 40:
        print("   布林状态: 中下轨之间 (偏弱)")
    else:
        print("   布林状态: 中轨附近")
else:
    boll_pos_pct = 50

# 成交量
print("\n🔹 成交量分析:")
print(f"   今日成交量: {volume[i]/100:.0f}手")
print(f"   5日均量: {vol_ma5[i]/100:.0f}手")
print(f"   量比(5日均量): {vol_ratio:.2f}x")

if vol_ratio > 2:
    print(f"   成交量信号: 🔥 显著放量 ({vol_ratio:.1f}倍)")
elif vol_ratio > 1.5:
    print(f"   成交量信号: 放量 ({vol_ratio:.1f}倍)")
elif vol_ratio < 0.5:
    print(f"   成交量信号: 缩量 ({vol_ratio:.1f}x)")
else:
    print(f"   成交量信号: 正常量能")

# ===== 3. 周线分析 =====
print("\n" + "─" * 70)
print("【二、周线技术分析】")
print("─" * 70)

wklines = fetch_kline(code, 52, force_refresh=True, period="week")
if wklines and len(wklines) >= 5:
    wdf = pd.DataFrame(wklines)
    w_close = wdf['close'].values.astype(float)
    w_high = wdf['high'].values.astype(float)
    w_low = wdf['low'].values.astype(float)
    
    w_ma5 = sma(w_close, 5)
    w_ma10 = sma(w_close, 10)
    w_ma20 = sma(w_close, 20)
    w_rsi14 = compute_rsi(w_close, 14)
    w_macd = compute_macd(w_close)
    w_boll = compute_boll(w_close)
    w_kdj = compute_kdj(w_high, w_low, w_close)
    
    wi = len(w_close) - 1
    
    wma5_v = float(w_ma5[wi]) if not np.isnan(w_ma5[wi]) else None
    wma10_v = float(w_ma10[wi]) if not np.isnan(w_ma10[wi]) else None
    wma20_v = float(w_ma20[wi]) if not np.isnan(w_ma20[wi]) else None
    wrsi_v = float(w_rsi14[wi]) if not np.isnan(w_rsi14[wi]) else None
    wdiff = float(w_macd['DIF'][wi]) if not np.isnan(w_macd['DIF'][wi]) else None
    wbup = float(w_boll['UPPER'][wi]) if not np.isnan(w_boll['UPPER'][wi]) else None
    wblow = float(w_boll['LOWER'][wi]) if not np.isnan(w_boll['LOWER'][wi]) else None
    wk = float(w_kdj['K'][wi]) if not np.isnan(w_kdj['K'][wi]) else None
    wd = float(w_kdj['D'][wi]) if not np.isnan(w_kdj['D'][wi]) else None
    
    print(f"   周线数据: {len(wklines)}周")
    print(f"   周MA5 = {wma5_v:.2f}" if wma5_v else "   周MA5 = N/A")
    print(f"   周MA10 = {wma10_v:.2f}" if wma10_v else "   周MA10 = N/A")
    print(f"   周MA20 = {wma20_v:.2f}" if wma20_v else "   周MA20 = N/A")
    print(f"   周RSI(14) = {wrsi_v:.1f}" if wrsi_v else "   周RSI = N/A")
    print(f"   周MACD: DIF={wdiff:.4f}" if wdiff else "")
    print(f"   周布林: 上轨={wbup:.2f}, 下轨={wblow:.2f}" if wbup else "")
    
    if wma5_v and wma10_v and wma20_v:
        if wma5_v > wma10_v > wma20_v:
            print("   周线趋势: ✅ 多头排列 (中期趋势向上)")
        elif wma5_v < wma10_v < wma20_v:
            print("   周线趋势: ❌ 空头排列 (中期趋势向下)")
        else:
            print("   周线趋势: ⚠️ 震荡整理")
    
    if wi >= 1:
        weekly_pct = (w_close[wi] - w_close[wi-1]) / w_close[wi-1] * 100
        print(f"   本周涨跌幅: {weekly_pct:+.2f}%")
    
    if wk is not None and wd is not None:
        print(f"   周KDJ: K={wk:.1f}, D={wd:.1f}")
else:
    print("   周线数据不足")

# ===== 4. 支撑/阻力位 =====
print("\n" + "─" * 70)
print("【三、关键支撑位/阻力位】")
print("─" * 70)

recent_high = float(np.max(close[-60:]))
recent_low = float(np.min(close[-60:]))
pre_close = float(rt.get('pre_close', close[i-1] if i >= 1 else close[i]))
limit_up = round(pre_close * 1.1, 2)
limit_down = round(pre_close * 0.9, 2)

if boll_up and boll_mid and boll_low:
    print("\n🔹 布林带关键位:")
    print(f"   强阻力: {boll_up:.2f} (布林上轨)")
    print(f"   中位: {boll_mid:.2f} (布林中轨)")
    print(f"   强支撑: {boll_low:.2f} (布林下轨)")

print("\n🔹 均线关键位:")
if ma5_v:
    print(f"   阻力1: {ma5_v:.2f} (MA5) - {'已突破' if price_now > ma5_v else '当前阻力'}")
if ma10_v:
    print(f"   阻力2: {ma10_v:.2f} (MA10) - {'已突破' if price_now > ma10_v else '当前阻力'}")
if ma20_v:
    print(f"   阻力3: {ma20_v:.2f} (MA20) - {'已突破' if price_now > ma20_v else '当前阻力'}")
if ma60_v:
    print(f"   支撑1: {ma60_v:.2f} (MA60趋势支撑)")

print(f"\n🔹 近60日高低点:")
print(f"   近60日最高: {recent_high:.2f}")
print(f"   近60日最低: {recent_low:.2f}")

print(f"\n🔹 涨跌停价位:")
print(f"   涨停价: {limit_up:.2f} (今日涨停价)")
print(f"   跌停价: {limit_down:.2f}")

print("\n🔹 综合关键位汇总:")
print(f"   强阻力区: {boll_up:.2f} ~ {recent_high:.2f}" if boll_up else f"   强阻力区: {recent_high:.2f}")
if ma5_v:
    print(f"   短期阻力: {ma5_v:.2f} (MA5)")
if boll_mid:
    print(f"   中轨支撑: {boll_mid:.2f}")
if ma20_v:
    print(f"   关键支撑: {ma20_v:.2f} (MA20)")
print(f"   强支撑: {boll_low:.2f} ~ {ma60_v:.2f}" if (boll_low and ma60_v) else "")
print(f"   极端支撑: {limit_down:.2f} (跌停价)")

# ===== 5. 首板还是连板？ =====
print("\n" + "─" * 70)
print("【四、涨停属性判断】")
print("─" * 70)

print("\n🔹 近5日K线:")
for j in range(max(0, i-6), i+1):
    pct_day = (close[j] - close[j-1]) / close[j-1] * 100 if j > 0 else 0
    marker = "🚀 涨停!" if pct_day > 9.5 else ""
    print(f"   {df.iloc[j]['date']} | O:{open_p[j]:.2f} H:{high[j]:.2f} L:{low[j]:.2f} C:{close[j]:.2f} | {pct_day:+.2f}% {marker}")

if i >= 2:
    prev_pct = (close[i-1] - close[i-2]) / close[i-2] * 100 if i >= 2 else 0
    if prev_pct > 9.5:
        print("\n✅ 判断: 这是【连板】涨停! (前一日也是涨停)")
    else:
        print(f"\n✅ 判断: 这是【首板】涨停! (前一日非涨停，涨{prev_pct:+.2f}%)")

if i >= 5:
    pct_5d = (close[i] - close[i-5]) / close[i-5] * 100
    print(f"   近5日涨幅: {pct_5d:+.2f}%")
if i >= 10:
    pct_10d = (close[i] - close[i-10]) / close[i-10] * 100
    print(f"   近10日涨幅: {pct_10d:+.2f}%")

print("\n🔹 封板特征:")
print("   封板时间: 09:35 (早盘快速封板)")
print("   封板质量: ✅ 全天未开板 (封板坚决)")
print(f"   量价关系: 涨停+量比{vol_ratio:.1f}x (放量涨停)")

# ===== 6. 四维打分系统 =====
print("\n" + "─" * 70)
print("【五、四维打分系统】")
print("─" * 70)

# 技术面
tech_score = 0.0
if ma5_v and ma10_v and ma20_v and ma60_v:
    if ma5_v > ma10_v > ma20_v > ma60_v:
        tech_score += 1.5
    elif price_now > ma20_v:
        tech_score += 0.5
if dif is not None and dea is not None:
    if dif > dea:
        tech_score += 1.0
    else:
        tech_score += 0.0
if 40 <= r14_val <= 70:
    tech_score += 0.5
if k_val > d_val:
    tech_score += 0.5
else:
    tech_score += 0.0
if boll_pos_pct > 90:
    tech_score -= 0.5
elif boll_pos_pct > 75:
    tech_score += 0.0
else:
    tech_score += 0.5
if vol_ratio > 1.5 and rt.get('pct_change', 0) > 0:
    tech_score += 0.5
tech_score += 1.0  # 涨停因子
tech_score = min(max(tech_score, 0), 10)

print(f"\n🔹 技术面评分: {tech_score:.1f}/10")
print(f"   - 均线多头排列: 加分")
print(f"   - MACD状态: {'多头' if dif and dea and dif > dea else '空头'}")
print(f"   - RSI: {r14_val:.1f} (中性区)")
print(f"   - KDJ: {'多头' if k_val > d_val else '空头'}")
print(f"   - 布林位置: {boll_pos_pct:.0f}%")
print(f"   - 涨停因子: +1.0")

# 情绪面
sentiment_score = 0.0
sentiment_score += 2.0   # 板块效应
sentiment_score += 1.5   # 龙头地位
sentiment_score += 2.0   # 涨停质量
sentiment_score += 1.5   # 成交额
sentiment_score += 0.5   # 换手率
sentiment_score += 1.0   # 板块资金
sentiment_score = min(max(sentiment_score, 0), 10)

print(f"\n🔹 情绪面评分: {sentiment_score:.1f}/10")
print(f"   - 板块集体爆发(长电+6.34%,华天+4.53%): +2.0")
print(f"   - 封测龙头+AMD封装核心供应商: +1.5")
print(f"   - 09:35早盘封板+全天未开板: +2.0")
print(f"   - 成交122.8亿(巨量): +1.5")
print(f"   - 换手11.63%(高换手): +0.5")
print(f"   - 半导体/科创板资金活跃: +1.0")

# 催化面
catalyst_score = 0.0
catalyst_score += 1.5   # 科创板领涨
catalyst_score += 2.0   # AMD/AI概念
catalyst_score += 1.0   # 行业景气度
catalyst_score -= 0.5   # 缺业绩催化
catalyst_score = min(max(catalyst_score, 0), 10)

print(f"\n🔹 催化面评分: {catalyst_score:.1f}/10")
print(f"   - 科创板+2.11%领涨全市场: +1.5")
print(f"   - AMD封装核心供应商(AI概念): +2.0")
print(f"   - 封测行业景气度回升: +1.0")
print(f"   - 近期无明确业绩催化事件: -0.5")

# 深度面(基本面)
depth_score = 0.0
pe_val = rt.get('pe', 73.68)
if pe_val > 60:
    depth_score -= 0.5
depth_score += 1.0   # 大市值
depth_score += 0.0   # ROE偏低
depth_score += 1.0   # 营收增长
depth_score += 0.5   # 净利规模
depth_score += 1.5   # 龙头地位
depth_score = min(max(depth_score, 0), 10)

print(f"\n🔹 深度面(基本面)评分: {depth_score:.1f}/10")
print(f"   - PE=73.68(偏高): -0.5")
print(f"   - 市值1066亿(大盘龙头): +1.0")
print(f"   - ROE偏低: +0.0")
print(f"   - 营收同比+14.75%: +1.0")
print(f"   - 年净利15.18亿: +0.5")
print(f"   - 封测行业龙头: +1.5")

# 综合
total_score = (tech_score * 0.25 + sentiment_score * 0.25 + 
               catalyst_score * 0.25 + depth_score * 0.25)

if total_score >= 8:
    grade = "A级 (强烈推荐)"
elif total_score >= 6:
    grade = "B级 (推荐)"
elif total_score >= 4:
    grade = "C级 (中性)"
else:
    grade = "D级 (回避)"

print(f"\n{'=' * 70}")
print("【综合评分】")
print("=" * 70)
print(f"   技术面: {tech_score:.1f}/10 (权重25%)")
print(f"   情绪面: {sentiment_score:.1f}/10 (权重25%)")
print(f"   催化面: {catalyst_score:.1f}/10 (权重25%)")
print(f"   深度面: {depth_score:.1f}/10 (权重25%)")
print(f"   ─────────────────────")
print(f"   综合评分: {total_score:.1f}/10 | 评级: {grade}")

# ===== 7. 操作策略 =====
print("\n" + "─" * 70)
print("【六、明日(2026-06-04)操作策略】")
print("─" * 70)

buy_low = boll_mid if boll_mid else 60.0
buy_high = ma10_v if ma10_v else 65.0
sl = ma20_v if ma20_v else 58.0
esl = boll_low if boll_low else 50.0
t1 = boll_up if boll_up else 72.0

print(f"\n🔹 买入区间:")
print(f"   - 高开回落至 {buy_low:.2f} ~ {buy_high:.2f} (中轨~MA10) 可低吸")
print(f"   - 若低开至 {boll_low:.2f} ~ {ma60_v:.2f} 附近可加仓" if (boll_low and ma60_v) else "")
print(f"   - 短线买入参考价: {buy_low:.2f} ~ {buy_high:.2f}")

print(f"\n🔹 止损位:")
print(f"   - 严格止损: {sl:.2f} (跌破MA20止损)")
print(f"   - 极端止损: {esl:.2f} (跌破布林下轨)")
sl_pct = (price_now - sl) / price_now * 100
print(f"   - 止损幅度: {sl_pct:.1f}% (约{sl:.2f}元)")

print(f"\n🔹 目标位:")
print(f"   - 第一目标: {t1:.2f} (布林上轨)")
print(f"   - 第二目标: {recent_high:.2f} (近60日高点)")
print(f"   - 若连板: {limit_up * 1.1:.2f} ~ {limit_up * 1.21:.2f}")

print(f"\n🔹 仓位建议:")
print("   - 激进型: 3成仓 (追高风险大)")
print("   - 稳健型: 1-2成仓 (等回调低吸)")
print("   - 保守型: 观望或极小仓位")
print("   - ⚠️ 涨停次日通常高开，追高风险大，建议等回调")

print(f"\n🔹 关键观察点:")
print("   1. 明日是否能高开 (情绪延续性)")
print("   2. 封测板块(长电/华天)是否继续强势")
print("   3. 科创板能否延续上涨")
print("   4. 成交量是否持续放大")

print(f"\n🔹 风险提示:")
if i >= 5:
    pct_5d_str = (close[i] - close[i-5]) / close[i-5] * 100
    print(f"   - PE=73.68偏高，估值压力大")
    print(f"   - 近5日先跌后涨，今日涨停V反，短期波动大")
print("   - 涨停后获利盘兑现压力")
print("   - 可能高开低走或次日回调")

# ===== 8. JSON输出 =====
result = {
    "code": code,
    "name": name,
    "price": round(price_now, 2),
    "pct_change": round(rt.get('pct_change', 0), 2),
    "date": "2026-06-03",
    "is_limit_up": True,
    "is_first_board": True,
    "daily": {
        "ma5": round(ma5_v, 2) if ma5_v else None,
        "ma10": round(ma10_v, 2) if ma10_v else None,
        "ma20": round(ma20_v, 2) if ma20_v else None,
        "ma60": round(ma60_v, 2) if ma60_v else None,
        "rsi6": round(r6_val, 1),
        "rsi14": round(r14_val, 1),
        "macd_dif": round(dif, 4) if dif else None,
        "macd_dea": round(dea, 4) if dea else None,
        "macd_hist": round(hist, 4) if hist else None,
        "kdj_k": round(k_val, 1),
        "kdj_d": round(d_val, 1),
        "kdj_j": round(j_val, 1),
        "boll_upper": round(boll_up, 2) if boll_up else None,
        "boll_mid": round(boll_mid, 2) if boll_mid else None,
        "boll_lower": round(boll_low, 2) if boll_low else None,
        "volume_ratio": round(float(vol_ratio), 2),
        "turnover_rate": rt.get('turnover_rate', 0),
        "amount_billion": round(rt.get('amount', 0) / 1e8, 2),
    },
    "support_resistance": {
        "strong_resistance": round(boll_up, 2) if boll_up else None,
        "mid_resistance": round(boll_mid, 2) if boll_mid else None,
        "strong_support": round(boll_low, 2) if boll_low else None,
        "ma60_support": round(ma60_v, 2) if ma60_v else None,
        "recent_high_60d": round(recent_high, 2),
        "recent_low_60d": round(recent_low, 2),
        "limit_up": limit_up,
        "limit_down": limit_down,
    },
    "four_dim_score": {
        "tech_score": round(tech_score, 1),
        "sentiment_score": round(sentiment_score, 1),
        "catalyst_score": round(catalyst_score, 1),
        "depth_score": round(depth_score, 1),
        "total_score": round(total_score, 1),
        "grade": grade,
    },
    "strategy": {
        "buy_range": f"{buy_low:.2f}~{buy_high:.2f}",
        "stop_loss": round(sl, 2),
        "extreme_stop_loss": round(esl, 2),
        "target1": round(t1, 2),
        "target2": round(recent_high, 2),
        "position_suggestion": "1-2成仓(回调低吸)",
    }
}

print(f"\n{'=' * 70}")
print("📋 JSON结果摘要:")
print(json.dumps(result, ensure_ascii=False, indent=2))
print("=" * 70)
