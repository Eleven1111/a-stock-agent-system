# 非涨停板板块趋势扫描

## 背景

2026-06-08 session 教训：用户问"今天其他热门板块还有吗"，仅用涨停板池回答，漏掉了煤炭板块（当日中国神华+4.72%、大有能源+7.62%，但0只涨停）。

**根因：** 涨停板池只统计封死涨停板的股票。机构/大资金驱动的趋势行情（涨4-7%但不封板）完全不可见。

## 触发条件

当用户问以下问题时，**必须**并行执行涨停板扫描 + 非涨停板板块趋势扫描：

- "今天什么板块热" / "还有哪些板块"（涨停数<60只时强制）
- "今天还有什么可以看的"
- 用户表现出对趋势风格（非打板）的兴趣
- 涨停板扫描没有覆盖到明显的市场异动（如大市值龙头涨超4%）

## 完整工作流

### Step 1：从涨停板数据中找线索

```python
import akshare as ak
df = ak.stock_zt_pool_em(date="20260608")
```

即使板块0涨停，从轮动数据中可能发现：
- **曾经热过的板块重新出现**（如煤炭06/01有9家涨停，随后退潮，06/08重新走强）
- **涨停板TOP5板块的产业链上下游**（如通用设备热→上游钢材/煤炭可能受益）

运行 `--rotation` 查看近5日板块变化：

```bash
~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/skills/hot-money-tactics/scripts/analyze.py --rotation
```

### Step 2：圈定候选板块

从这些来源找候选：

| 线索类型 | 示例 | 说明 |
|:--------|:----|:----|
| 轮动历史 | 煤炭06/01有9涨停，后退潮，现重新走强 | 二次点火信号 |
| 大市值龙头异动 | 中国神华+4.72% | 大块头一动说明机构进场 |
| 商品/期货联动 | 动力煤期货大涨、焦煤期货异动 | 上游价格驱动 |
| 季节性因素 | 6月入夏→用电高峰→煤炭需求增加 | 日历效应 |
| 宏观经济信号 | 降息预期→地产/周期板块受益 | 政策驱动 |
| 用户提及 | 用户说"煤炭"、"有色"、"钢铁" | 按名称直接扫 |

### Step 3：列出板块成分股并批量扫描

对每个候选板块，手动列出核心成分股（15-25家），用腾讯 API 批量查：

```python
import os, urllib.request
os.environ["NO_PROXY"] = ".gtimg.cn"

# 例：煤炭板块22只核心成分股
coal_stocks = {
    'sh600121': '郑州煤电', 'sh600123': '兰花科创', 'sh600188': '兖矿能源',
    'sh600348': '华阳股份', 'sh600395': '盘江股份', 'sh600403': '大有能源',
    'sh600508': '上海能源', 'sh600971': '恒源煤电', 'sh600985': '淮北矿业',
    'sh601001': '晋控煤业', 'sh601088': '中国神华', 'sh601101': '昊华能源',
    'sh601225': '陕西煤业', 'sh601666': '平煤股份', 'sh601699': '潞安环能',
    'sh601898': '中煤能源', 'sh601918': '新集能源', 'sz000552': '甘肃能化',
    'sz000723': '美锦能源', 'sz000937': '冀中能源', 'sz000983': '山西焦煤',
    'sz002128': '电投能源',
}

codes_str = ','.join(coal_stocks.keys())
url = f'http://qt.gtimg.cn/q={codes_str}'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=10) as resp:
    raw = resp.read().decode('gbk', errors='ignore')

results = []
for line in raw.strip().split(';'):
    if not line or '=' not in line: continue
    val = line.split('=', 1)[1].strip().strip('"').split('~')
    if len(val) < 40: continue
    name = val[1]
    code = val[2]
    price = float(val[3]) if val[3] else 0
    chg_pct = float(val[32]) if val[32] else 0
    amt_yi = float(val[37]) / 10000 if val[37] else 0
    pe = val[39] if val[39] and val[39] != '0.00' else '-'
    total_mcap = float(val[45]) if val[45] else 0
    results.append((name, code, price, chg_pct, amt_yi, pe, total_mcap))
```

### Step 4：板块评估指标

```python
results.sort(key=lambda x: -x[3])
up_count = sum(1 for r in results if r[3] > 0)
avg_chg = sum(r[3] for r in results) / len(results)
total_amt = sum(r[5] for r in results)

print(f"上涨比例: {up_count}/{len(results)} = {up_count/len(results)*100:.0f}%")
print(f"平均涨幅: {avg_chg:+.2f}%")
print(f"板块总成交: {total_amt:.0f}亿")
```

| 指标 | 强信号 | 弱信号 |
|:----|:------|:------|
| 上涨比例 | > 60% | < 40% |
| 平均涨幅 | > 1.5% | < 0.5% |
| 板块总成交 | > 80亿 | < 30亿 |
| 龙头涨幅 | > 5% | < 2% |
| 板块内部分化 | 普涨（多数上涨） | 分化严重（仅龙头涨） |

### Step 5：优质标的筛选

拉出K线做均线判断（腾讯 ifzq API）：

```python
import subprocess, json
r = subprocess.run(['curl', '-sL', '--connect-timeout', '10',
    f'https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh601088,day,,,60,qfq'],
    capture_output=True, text=True, timeout=15)
data = json.loads(r.stdout)
entry = list(data['data'].values())[0]
day_data = entry.get('qfqday') or entry.get('day')

closes = [float(d[2]) for d in day_data if isinstance(d, list) and len(d) >= 6]
ma5 = sum(closes[-5:])/5
ma10 = sum(closes[-10:])/10
ma20 = sum(closes[-20:])/20
ma60 = sum(closes[-60:])/60
```

优质趋势标的特征：
- **多头排列**（MA5>MA10>MA20>MA60）✅
- **紧贴MA5**（偏离<+3%，未过热）✅
- **PE合理**（非亏损、非泡沫估值）✅
- **缩量/温和放量上涨**（量比<1.5x，无分歧）✅
- **60日区间位置**：<80%分位（未到顶部）或有突破信号

排除：
- 🚫 10日涨幅>30%的——短期过热
- 🚫 距MA5>+10%的——追高风险大
- 🚫 PE为负或>100的亏损/泡沫股（纯情绪投机除外）

### Step 6：最终输出格式

```
## 板块名 — 趋势扫描

**板块表现：** N只上涨/N只总计 = X%, 平均涨幅+XX%, 总成交XX亿

| 标的 | 现价 | 1手价 | PE | 今日涨幅 | 均线 | 60日位置 | 评价 |
|:---|:----|:-----|:--|:--------|:----|:--------|:----|

**首选标的分析（含明日竞价计划）**
**排除说明（每个排除标的给原因）**
```

## 已知的板块成分股列表

以下板块的完整成分股列表可按需使用。列表来源于东方财富行业分类。

### 煤炭开采（22只）
`sh600121`郑州煤电, `sh600123`兰花科创, `sh600188`兖矿能源, `sh600348`华阳股份, `sh600395`盘江股份, `sh600403`大有能源, `sh600508`上海能源, `sh600971`恒源煤电, `sh600985`淮北矿业, `sh601001`晋控煤业, `sh601088`中国神华, `sh601101`昊华能源, `sh601225`陕西煤业, `sh601666`平煤股份, `sh601699`潞安环能, `sh601898`中煤能源, `sh601918`新集能源, `sz000552`甘肃能化, `sz000723`美锦能源, `sz000937`冀中能源, `sz000983`山西焦煤, `sz002128`电投能源

### 通用设备（部分龙头）
`sz001696`宗申动力, `sz002164`宁波东力, `sz002931`锋龙股份, `bj920510`丰光精密, `sz002046`国机精工

### 房地产开发（部分龙头）
`sz000517`荣安地产, `sz000620`盈新发展, `sz002314`南山控股, `sh601588`北辰实业

### 自动化设备（部分龙头）
`sh603203`快克智能, `sz002747`埃斯顿, `bj920578`巨能股份
