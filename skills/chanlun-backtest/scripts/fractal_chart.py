#!/usr/bin/env python3
"""
缠论分形图绘制脚本 — 腾讯K线数据 + 顶底分型检测 + Markdown输出。

用法:
  python3 fractal_chart.py 300255 常山药业
  python3 fractal_chart.py 603859 能科科技 --days 60

输出: 含分形图的 Markdown，可直接输出到终端或管道到文件。

依赖: 无第三方库（仅用 urllib + json）
数据源: 腾讯 ifzq.gtimg.cn（前复权日线，免费全天候）
"""
import os
import sys
import urllib.request
import json

os.environ["NO_PROXY"] = ".gtimg.cn,.eastmoney.com"

import argparse
parser = argparse.ArgumentParser(description="缠论分形图")
parser.add_argument("code", help="股票代码，如 300255")
parser.add_argument("name", nargs="?", default="", help="股票名称")
parser.add_argument("--days", type=int, default=60, help="K线天数（默认60）")
parser.add_argument("--height", type=int, default=16, help="图表高度行数")
args = parser.parse_args()

CODE = args.code
NAME = args.name if args.name else CODE
DAYS = min(args.days, 120)
CHART_HEIGHT = args.height

# ===== 1. 腾讯K线 =====
PREFIX = "sh" if CODE.startswith("6") else "sz"
url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={PREFIX}{CODE},day,,,{DAYS * 2},qfq"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    data = raw.get("data", {})
    klines = data.get(f"{PREFIX}{CODE}", {}).get("qfqday", [])
except Exception as e:
    print(f"❌ 数据获取失败: {e}")
    sys.exit(1)

if not klines or len(klines) < 5:
    print("❌ 数据不足")
    sys.exit(1)

# 解析 — 注意腾讯API格式: [date, open, close, high, low, volume_or_amount]
# 今日K线的volume字段可能是dict(含除权信息)，需特殊处理
bars = []
for k in klines[-DAYS:]:
    date, o, c, h, low_value = str(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])
    v_raw = k[5]
    if isinstance(v_raw, (int, float)):
        v = int(v_raw)
    elif isinstance(v_raw, str):
        v = int(float(v_raw))
    else:
        v = 0  # dict or None — 含除权信息
    bars.append({"date": date, "open": o, "close": c, "high": h, "low": low_value, "vol": v})

n = len(bars)
closes = [b["close"] for b in bars]
highs = [b["high"] for b in bars]
lows = [b["low"] for b in bars]
opens = [b["open"] for b in bars]
dates = [b["date"] for b in bars]


def sma(arr, period):
    res = []
    for i in range(len(arr)):
        if i < period - 1:
            res.append(None)
        else:
            res.append(sum(arr[i - period + 1:i + 1]) / period)
    return res


ma5 = sma(closes, 5)
ma10 = sma(closes, 10)
ma20 = sma(closes, 20)
ma60 = sma(closes, 60)

# ===== 2. 缠论分型检测 =====
# 顶分型: high[i] > high[i-1] AND high[i] > high[i+1]
# 底分型: low[i] < low[i-1] AND low[i] < low[i+1]
# 合并规则: 相邻的顶分型取最高，相邻的底分型取最低
raw_tops = []
raw_bottoms = []
for i in range(1, n - 1):
    if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
        raw_tops.append(i)
    if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
        raw_bottoms.append(i)

top_fractals = []
for idx in raw_tops:
    if not top_fractals:
        top_fractals.append((idx, highs[idx]))
    else:
        last_idx, last_high = top_fractals[-1]
        if idx - last_idx <= 2:
            if highs[idx] > last_high:
                top_fractals[-1] = (idx, highs[idx])
        else:
            top_fractals.append((idx, highs[idx]))

bottom_fractals = []
for idx in raw_bottoms:
    if not bottom_fractals:
        bottom_fractals.append((idx, lows[idx]))
    else:
        last_idx, last_low = bottom_fractals[-1]
        if idx - last_idx <= 2:
            if lows[idx] < last_low:
                bottom_fractals[-1] = (idx, lows[idx])
        else:
            bottom_fractals.append((idx, lows[idx]))

# ===== 3. 绘制 =====
min_price = min(lows)
max_price = max(highs)
price_range = max_price - min_price
if price_range == 0:
    price_range = 1

lines = []

# 标题区
lines.append(f"\n## 📊 缠论分形图 — {NAME}({CODE}) 近{DAYS}日")
lines.append(f"📅 期间: {dates[0]} ~ {dates[-1]}")
lines.append(f"📈 区间: {min_price:.2f} ~ {max_price:.2f}  最新: {closes[-1]:.2f}")
ma_parts = []
for label, arr in [("MA5", ma5), ("MA10", ma10), ("MA20", ma20), ("MA60", ma60)]:
    v = arr[-1]
    ma_parts.append(f"{label}: {v:.2f}" if v is not None else f"{label}: N/A")
avg_vol = sum(b["vol"] for b in bars) // n
lines.append("📉 " + "  ".join(ma_parts) + f"  (日均量: {avg_vol})")
lines.append("")

# 排列判断
vals_ok = [ma5[-1], ma10[-1], ma20[-1]]
if all(v is not None for v in vals_ok):
    if vals_ok[0] > vals_ok[1] > vals_ok[2]:
        lines.append("✅ **多头排列** (MA5>MA10>MA20)")
    elif vals_ok[0] < vals_ok[1] < vals_ok[2]:
        lines.append("❌ **空头排列** (MA5<MA10<MA20)")
    else:
        lines.append("🌀 **均线缠绕**")
else:
    lines.append("🌀 均线计算中")
lines.append("")

# K线图
price_labels = [max_price - (price_range * i / max(CHART_HEIGHT - 1, 1)) for i in range(CHART_HEIGHT)]
max_cols = min(n, 65)

for row_idx in range(CHART_HEIGHT):
    price_level = price_labels[row_idx]
    row_chars = []
    for col_idx in range(max_cols):
        o, h, low_value, c = opens[col_idx], highs[col_idx], lows[col_idx], closes[col_idx]
        is_up = c >= o
        in_range = low_value <= price_level <= h
        if not in_range:
            row_chars.append(" ")
            continue
        if is_up:
            row_chars.append("█" if o <= price_level <= c else "|")
        else:
            row_chars.append("▓" if c <= price_level <= o else "|")

    label = ""
    if row_idx == 0:
        label = f"{max_price:>8.2f} ┤"
    elif row_idx == CHART_HEIGHT - 1:
        label = f"{min_price:>8.2f} ┤"
    elif row_idx == CHART_HEIGHT // 2:
        label = f"{(max_price + min_price) / 2:>8.2f} ┤"
    else:
        label = " " * 11
    lines.append(f"{label}{''.join(row_chars)}")

lines.append(" " * 11 + "└" + "─" * max_cols)

# 日期轴
step = max(1, max_cols // 8)
date_str = " " * 11
for i in range(0, max_cols, step):
    d = dates[i][5:10]
    date_str += d + " " * (step - len(d) + 1) if step > len(d) else d + " "
lines.append(date_str)

# 分型标记
top_flags = ["  "] * max_cols
bottom_flags = ["  "] * max_cols
for idx, _ in top_fractals:
    if idx < max_cols:
        top_flags[idx] = "↑ "
for idx, _ in bottom_fractals:
    if idx < max_cols:
        bottom_flags[idx] = "↓ "

lines.append(" " * 11 + "".join(top_flags))
lines.append(" " * 11 + "".join(bottom_flags))
lines.append("\n> ↑ 顶分型  ↓ 底分型  █ 阳线  ▓ 阴线  | 影线")
lines.append("")

# 分型明细
lines.append("### 分型明细")
lines.append("\n| # | 日期 | 类型 | 价格 |")
lines.append("|:---|:---|:---:|:---:|")
top_idx = 0
bot_idx = 0
for i in range(n):
    if top_idx < len(top_fractals) and top_fractals[top_idx][0] == i:
        _, p = top_fractals[top_idx]
        lines.append(f"| {i + 1} | {dates[i]} | 🔺顶分型 | {p:.2f} |")
        top_idx += 1
    if bot_idx < len(bottom_fractals) and bottom_fractals[bot_idx][0] == i:
        _, p = bottom_fractals[bot_idx]
        lines.append(f"| {i + 1} | {dates[i]} | 🔻底分型 | {p:.2f} |")
        bot_idx += 1

# 统计
total_chg = (closes[-1] - closes[0]) / closes[0] * 100
recent5 = (closes[-1] - closes[-5]) / closes[-5] * 100 if n >= 5 else 0
arrow1 = "🟢" if total_chg > 0 else "🔴"
arrow2 = "🟢" if recent5 > 0 else "🔴"
lines.append(f"\n📊 区间涨跌: {arrow1} {total_chg:+.2f}% | 近5日: {arrow2} {recent5:+.2f}%")
lines.append(f"🏔️ 顶分型: {len(top_fractals)}个 | 🏞️ 底分型: {len(bottom_fractals)}个")
lines.append(f"📈 最高: {max_price:.2f} (#{highs.index(max_price) + 1}, {dates[highs.index(max_price)]})")
lines.append(f"📉 最低: {min_price:.2f} (#{lows.index(min_price) + 1}, {dates[lows.index(min_price)]})")

output = "\n".join(lines)
print(output)
