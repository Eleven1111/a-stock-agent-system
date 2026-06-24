# Non-Limit-Up Sector Trend Scan

A limit-up pool does not represent all market leadership. Institution-led
trends may have broad participation and high turnover without any stock closing
at the limit.

## When To Run

Run the trend scan alongside the limit-up scan when:

- the request asks for market-wide sector leadership;
- the limit-up pool is unusually small;
- large-cap leaders show material moves;
- commodity, policy, or global inputs imply a sector transmission path.

## Workflow

1. Enumerate current industry and concept boards from a provider adapter.
2. Add active runtime sector/theme subscriptions.
3. Pull current constituents from the provider; do not use a static list.
4. Batch current quotes and calculate breadth, median return, turnover, and
   concentration.
5. Compare with recent limit-up counts and prior run snapshots.
6. For the top bounded sectors, fetch K-line, announcement, and risk evidence
   for representative securities.

## Sector Metrics

- advancing share and median return;
- total and median turnover;
- leader contribution versus broad participation;
- number and quality of limit-up stocks;
- multi-day persistence and reversal;
- cross-market or commodity confirmation;
- source coverage and stale-data ratio.

A sector with one strong leader and weak breadth is not equivalent to broad
sector strength.

## Security Filters

Positive trend evidence should still be rejected or downgraded for:

- excessive short-term extension;
- weak liquidity or untradeable state;
- unresolved announcement risk;
- deteriorating profit or cash flow;
- portfolio concentration limits;
- missing or stale required datasets.

## Output

Report the scanned universe, source timestamp, coverage, ranked sectors,
breadth, representative securities, exclusions, and uncertainty. The scan
produces evidence for Triage; it does not bypass strategy or portfolio policy.

---

## 实操：候选圈定 + 腾讯 API 批量扫描

何时执行：用户问"今天什么板块热"且涨停数偏少（<60 只），或用户表现出对趋势风格（非打板）的兴趣时。

**典型案例：** 2026-06-08 煤炭板块走强（中国神华 +4.72%、大有能源 +7.62%）但 **0 只涨停**，涨停板池完全不可见。原因是机构/大资金驱动的板块行情——不靠封板，靠趋势。

### 第一步：圈定候选板块

从这些线索中找：
- 涨停板池中板块热度排名的邻居板块（同产业链上下游）
- 历史轮动数据中曾经热过的板块（如 06/01 煤炭 9 家涨停）
- 当日宏观/政策/商品异动（如期货大涨、夏季用电高峰、煤价上涨等）
- 大市值龙头异动（如当天中国神华 +4.72% 是明显的板块信号）

### 第二步：拉成分股批量扫描

对候选板块手动列出核心成分股（15~25 家），用腾讯 API 批量查：

```python
import os, urllib.request
os.environ["NO_PROXY"] = ".gtimg.cn"

codes = ",".join(["sh601088","sh601225","sh600403",...])  # 板块成分股
url = f'http://qt.gtimg.cn/q={codes}'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=10) as resp:
    raw = resp.read().decode('gbk', errors='ignore')

results = []
for line in raw.strip().split(';'):
    val = line.split('=',1)[1].strip().strip('"').split('~')
    name, price, chg_pct, amt_yi, pe, total_mcap = \
        val[1], float(val[3]), float(val[32]), float(val[37])/10000, val[39], float(val[45])
    results.append((name, price, chg_pct, amt_yi, pe, total_mcap))
```

### 板块评估指标（阈值）

- **上涨比例** > 50% → 板块效应强
- **平均涨幅** > 1.5% → 资金流入明显
- **板块总成交** > 50亿 → 有机构参与
- **龙头涨幅** > 5% → 有带头大哥
