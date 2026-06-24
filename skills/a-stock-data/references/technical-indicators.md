# 技术指标与选股策略

> ⚠️ **talib 未安装**：本机 macOS 12 没有 ta-lib，下面 talib 代码仅作参考。
> **优先用 `stock-analyst` skill 的纯 numpy 实现**（无需 ta-lib）：
> ```bash
> ~/.hermes/hermes-agent/venv/bin/python3 \
>   ~/.hermes/skills/stock-analyst/analyst.py analyze 600519 贵州茅台
> ```
> 该工具内置 MA/MACD/RSI/KDJ/布林带全部用 numpy 计算，见 `scripts/tech_analysis.py`。
> 仅当确实需要 talib 时才 `pip install ta-lib`。

## 常用指标（talib 参考）

```python
import talib
import numpy as np

close = np.array(df['收盘'], dtype=float)
high = np.array(df['最高'], dtype=float)
low = np.array(df['最低'], dtype=float)
volume = np.array(df['成交量'], dtype=float)

# 均线
df['MA5'] = talib.MA(close, timeperiod=5)
df['MA20'] = talib.MA(close, timeperiod=20)
df['MA60'] = talib.MA(close, timeperiod=60)

# MACD
macd, signal, hist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)

# RSI
df['RSI_6'] = talib.RSI(close, timeperiod=6)

# KDJ
k, d = talib.STOCH(high, low, close, fastk_period=9, slowk_period=3, slowd_period=3)
df['KDJ_J'] = 3 * k - 2 * d

# 布林带
upper, middle, lower = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2)

# 成交量
df['VOL_MA5'] = talib.MA(volume, timeperiod=5)
df['VOL_RATIO'] = df['成交量'] / df['VOL_MA5']
```

## 选股策略

```python
# 策略1：均线金叉（MA5 上穿 MA20）
df['SIG_MA_GOLD'] = (df['MA5'] > df['MA20']) & (df['MA5'].shift(1) <= df['MA20'].shift(1))

# 策略2：MACD 金叉
df['SIG_MACD_GOLD'] = (df['MACD'] > df['MACD_SIGNAL']) & (df['MACD'].shift(1) <= df['MACD_SIGNAL'].shift(1))

# 策略3：RSI 超卖
df['SIG_RSI_OVERSOLD'] = df['RSI_6'] < 30

# 策略4：布林带突破
df['SIG_BOLL_BREAK'] = df['收盘'] > df['BOLL_UPPER']

# 策略5：综合多因子
df['SIG_MULTI'] = (
    (df['MA5'] > df['MA20']) &          # 趋势向上
    (df['RSI_6'] > 50) &                # 不超卖
    (df['MACD'] > df['MACD_SIGNAL']) &  # MACD 金叉
    (df['VOL_RATIO'] > 1.5)             # 放量
)
```

## 批量选股流程

```python
import akshare as ak
import talib, numpy as np, pandas as pd

stock_list = ak.stock_zh_a_spot_em()
results = []
for _, row in stock_list.iterrows():
    code, name = row['代码'], row['名称']
    df = ak.stock_zh_a_hist(symbol=code, period="daily")
    if df.empty:
        continue
    close = np.array(df['收盘'], dtype=float)
    ma5, ma20 = talib.MA(close, 5)[-1], talib.MA(close, 20)[-1]
    rsi6 = talib.RSI(close, 6)[-1]
    macd, signal, _ = talib.MACD(close, 12, 26, 9)
    if ma5 > ma20 and rsi6 > 50 and macd[-1] > signal[-1]:
        results.append({'代码': code, '名称': name, '现价': row['最新价'],
                        'MA5': ma5, 'MA20': ma20, 'RSI': rsi6, '涨跌幅': row['涨跌幅']})

print(pd.DataFrame(results).sort_values('涨跌幅', ascending=False))
```

## 涨停板查询

```python
import akshare as ak

df = ak.stock_zh_a_spot_em()
df_zt = df[df['涨跌幅'] >= 9.9].sort_values('涨跌幅', ascending=False)   # 涨停
df_dt = df[df['涨跌幅'] <= -9.9].sort_values('涨跌幅', ascending=True)   # 跌停
print(f"涨停 {len(df_zt)} / 跌停 {len(df_dt)}")

# 更可靠：用专用涨停板池（不走 push2，CDN 稳定）
ak.stock_zt_pool_em(date="20250619")        # 连板数、封板资金、炸板次数、所属行业
ak.stock_zt_pool_strong_em(date="20250619")  # 强势股池
ak.stock_zt_pool_dt_em(date="20250619")      # 跌停板池
```
