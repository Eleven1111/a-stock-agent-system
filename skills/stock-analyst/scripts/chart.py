"""
K线图表输出模块（终端ASCII）
"""
import os
import sys
import numpy as np

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.data_cache import fetch_kline
from scripts.tech_analysis import sma


def draw_kline_chart(code: str, name: str = "", days=60, width=60, height=14):
    """终端ASCII K线图"""
    klines = fetch_kline(code, days)
    if not klines or len(klines) < 5:
        return f"[{code}] 数据不足"
    
    data = klines[-days:]
    n = len(data)
    
    closes = np.array([k['close'] for k in data])
    highs = np.array([k['high'] for k in data])
    lows = np.array([k['low'] for k in data])
    opens = np.array([k['open'] for k in data])
    
    min_price = min(lows)
    max_price = max(highs)
    price_range = max_price - min_price
    if price_range == 0:
        price_range = 1
    
    lines = [f"\n📊 {name}({code}) 近{n}日K线"]
    lines.append(f"   最高: {max_price:.2f}  最低: {min_price:.2f}  最新: {closes[-1]:.2f}")
    
    # MA计算
    ma5 = sma(closes, 5)
    ma20 = sma(closes, 20)
    ma5_str = f"MA5:{ma5[-1]:.2f}" if not np.isnan(ma5[-1]) else ""
    ma20_str = f"MA20:{ma20[-1]:.2f}" if not np.isnan(ma20[-1]) else ""
    lines.append(f"   {ma5_str}  {ma20_str}")
    lines.append("")
    
    # 绘制K线
    chart_height = max(height, 8)
    
    # 标尺行
    price_labels = []
    for i in range(chart_height):
        price_level = max_price - (price_range * i / max(chart_height - 1, 1))
        price_labels.append(price_level)
    
    for row_idx in range(chart_height):
        price_level = price_labels[row_idx]
        row_chars = []
        
        for i in range(n):
            o, h, low_value, c = data[i]['open'], data[i]['high'], data[i]['low'], data[i]['close']
            is_up = c >= o

            in_range = low_value <= price_level <= h
            if not in_range:
                row_chars.append(" ")
                continue
            
            # 在价格范围内
            if is_up:  # 阳线
                if o <= price_level <= c:
                    row_chars.append("█")
                else:
                    row_chars.append("│")
            else:  # 阴线或平
                if c <= price_level <= o:
                    row_chars.append("▓")
                else:
                    row_chars.append("│")
        
        # 价格标尺
        label = ""
        if row_idx == 0:
            label = f"{max_price:>8.2f} ┤"
        elif row_idx == chart_height - 1:
            label = f"{min_price:>8.2f} ┤"
        elif row_idx == chart_height // 2:
            mid_price = (max_price + min_price) / 2
            label = f"{mid_price:>8.2f} ┤"
        else:
            label = " " * 11
        
        lines.append(f"{label}{''.join(row_chars)}")
    
    # 底部
    lines.append(" " * 11 + "└" + "─" * min(n, 60))
    
    # 日期
    date_labels = []
    step = max(1, n // 6)
    for i in range(0, n, step):
        date_labels.append(data[i]['date'][5:10])
    line_len = min(n, 60)
    date_str = " " * 11 + "  ".join(date_labels)
    lines.append(date_str)
    
    # 涨跌幅
    if n > 1:
        total_pct = (closes[-1] - closes[0]) / closes[0] * 100
        recent_pct = (closes[-1] - closes[-5]) / closes[-5] * 100 if n >= 5 else 0
        arrow = "🟢" if total_pct > 0 else "🔴"
        arrow2 = "🟢" if recent_pct > 0 else "🔴"
        lines.append(f"\n   区间: {arrow}{total_pct:+.2f}% | 近5日: {arrow2}{recent_pct:+.2f}%")
    
    return "\n".join(lines)


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    name = sys.argv[2] if len(sys.argv) > 2 else ""
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    print(draw_kline_chart(code, name, days))
