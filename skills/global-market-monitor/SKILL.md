---
name: global-market-monitor
description: >-
  全球市场监控模块。监控美股指数/期货/VIX/国债/外汇/大宗商品/中概股ADR/
  关键科技股/美股行业ETF/全球指数，自动检测自然灾害、地缘政治事件和重大新闻，
  通过14项影响评估引擎输出A股板块预警。触发方式：盘前自动 cron、用户主动查询。
version: 1.1.0
author: Luna
metadata:
  hermes:
    tags: [全球市场, 美股, 期货, 外汇, VIX, 地缘政治, A股联动]
    category: finance
---

# Global Market Monitor — 全球市场监控

填补 A 股全栈分析系统的外围缺口。A 股不是孤岛——隔夜美股、大宗商品、汇率、地缘政治的每一次异动都在重塑次日 A 股的开盘格局。

## 为什么需要这个模块

A 股全栈 Agent 系统现有的 5 个 skill（stock-analyst / hot-money-tactics / news-to-sector / serenity / stock-triage）覆盖了**国内市场内部分析**的完整链路，但缺少**外围输入**：

> "美国科技股暴跌 → 次日 A 股 AI/半导体开盘承压" 这条传导链，现有系统感知不到。

本模块补上这个缺口。

## 监控范围

### 核心指标（每日必查）
- 🇺🇸 **美股三大指数**：标普500 / 纳斯达克 / 道琼斯（涨跌幅 + 纳指vs道指分化）
- 😱 **VIX 恐慌指数**：市场情绪温度计
- 🏦 **美债收益率**：10Y / 2Y（影响成长股估值）
- 🛢️ **大宗商品**：原油、黄金、铜、天然气 + 农产品
- 💱 **外汇**：美元/人民币、美元指数

### 结构信号（板块映射）
- 🇺🇸 **美股行业ETF**：XLK/XLE/XLF/... → 映射到 A 股对应板块
- 🔧 **关键科技股**：NVDA/AAPL/MSFT/TSLA/AMD/SMCI → A 股产业链联动
- 🇨🇳 **中概股 ADR**：BABA/JD/PDD/BIDU/NIO → 外资对中国的情绪温度计

### 环境信号（定时+突发）
- 🌏 **全球指数**：恒生/日经/韩国/欧洲
- 🌍 **地缘政治**：冲突/制裁/贸易摩擦/政策转向
- 🌪️ **自然灾害**：地震/飓风/洪水 → 供应链扰动

## 使用

### 命令行

```bash
PY=~/.hermes/hermes-agent/venv/bin/python3
MONITOR=~/.hermes/skills/global-market-monitor/scripts/monitor.py

# JSON输出（默认，供下游消费）
$PY $MONITOR

# 人类可读摘要
$PY $MONITOR --summary

# 完整数据 + 新闻
$PY $MONITOR --all
```

### 在 A 股分析流程中调用

```python
# 盘前分析时先加载全球数据
import json, subprocess
result = subprocess.run(
    ["python3", "scripts/monitor.py"],
    capture_output=True, text=True, cwd="~/.hermes/skills/global-market-monitor"
)
global_data = json.loads(result.stdout)

# 检查是否有高风险信号
for alert in global_data["impact"]["alerts"]:
    if "🔴" in alert["level"]:
        print(f"⚠️ 高风险: {alert['msg']}")
```

### Discord 快捷指令

直接在 Discord 输入 `/global` 即可触发全球市场扫描（需配置 stock-triage 支持）。

## 影响评估引擎（14项检查）

`assess_impact()` 自动执行：

1. **VIX 水平** — ≥30 触发全面警报，≥25 触发成长股承压警告
2. **美股指数涨跌幅** — >2% 标记为重大，>1% 标记为值得关注
3. **纳指 vs 道指分化** — 识别成长/价值风格轮动
4. **美债收益率变动** — >5bp 变动触发估值压力分析
5. **人民币汇率** — >0.5% 变动触发北向资金分析
6. **原油价格** — >3% 变动触发产业链分析
7. **黄金价格** — >2% 变动触发避险情绪分析
8. **中概股 ADR** — >3% 异动标记
9. **关键科技股** — >5% 异动标记
10. **铜价** — 铜博士，全球需求预期指标
11. **美股行业ETF** 🆕 — >2%异动联动A股对应板块（XLK→AI/半导体，XLE→石油/煤炭...）
12. **全球指数** 🆕 — 恒生>2%同向预警，日经/韩国亚太传导，欧洲信号
13. **自然灾害** 🆕 — USGS地震≥6级 + GDACS气旋/洪水/火山/海啸，按位置→A股板块
14. **重大新闻关键词** 🆕 — 从SerpAPI新闻中提取地缘冲突/美联储动态关键词自动告警

输出格式：
```json
{
  "alerts": [
    {"level": "🔴 高", "msg": "...", "sectors": [...], "action": "..."}
  ],
  "sector_impact": {"AI算力": -3, "半导体": -2, ...},
  "summary": "一句话总结"
}
```

完整的影响映射表见 `references/impact_mapping.md`（涵盖美股指数→A股、VIX→A股、美债→A股、汇率→A股、大宗商品→A股、行业ETF→板块、个股→产业链、地缘政治→板块、自然灾害→A股 共11个维度）。

## Cron 调度

### 盘前全球扫描（08:15，工作日）
在 A 股开盘前 75 分钟，汇总：
- 隔夜美股收盘数据
- 盘后期货/亚洲早盘走势
- VIX/美债/汇率最新状态
- 对今日 A 股开盘的影响预判

→ 比 BuilderPulse(08:30) 和 PulseEngine(08:55) 更早，为全天交易提供外围背景。

### 晚间全球扫描（22:30，工作日）
- 欧洲收盘 + 美股盘中
- 全球期货/汇率走势
- 当日重大国际新闻
- 次日市场关注事项

→ 在睡前提供全球市场全景，为次日做准备。

## 与现有 Skill 的集成

```
Cron 流水线              →   全球市场监控         →   stock-triage
                                                      ↓
08:15 全球盘前扫描 ──────────→ 输出 JSON ──────────→ Triage 评分升级判断
                                                      ↓
08:30 BuilderPulse                                       ├─ VIX≥30: 全市场 S 级
08:55 PulseEngine                                        ├─ 纳斯达克暴跌>2%: AI/半导体 S 级
10:00 高温主题                                           ├─ 中概ADR集体异动: 互联网 S 级
...                                                      └─ 原油暴涨>5%: 能源 S 级
15:25 收盘Triage
```

**新增信号升级规则（加入 stock-triage skill）：**

| 信号 | 条件 | 评分 | 优先级 |
|------|------|------|--------|
| 全球恐慌 | VIX ≥ 30 | 8 | 最高 |
| 美股科技暴跌 | 纳斯达克跌幅 ≥ 2% | 7 | 高 |
| 美股全面暴跌 | 标普500跌幅 ≥ 2% | 7 | 高 |
| 中概股集体异动 | ≥3只ADR涨/跌>5% | 6 | 中 |
| 关键科技股异动 | NVDA/AAPL 涨跌>5% | 5 | 中 |
| 原油暴涨 | WTI +5% | 6 | 中 |
| 人民币异动 | USD/CNY 波动>1% | 5 | 中 |
| 美债异动 | 10Y 变动>10bp | 5 | 中 |
| 地缘政治升级 | 重大冲突/制裁 | 10 | 最高 |

## 数据源

| 数据 | 主源 | 备用 |
|------|------|------|
| 美股指数 | yfinance (Yahoo Finance) | 新浪财经 `hq.sinajs.cn` |
| VIX | yfinance | — |
| 美债收益率 | yfinance | — |
| 美股行业ETF | yfinance | — |
| 全球指数 | yfinance | — |
| 大宗商品 | yfinance | — |
| 外汇 | yfinance | — |
| 关键个股/ADR | yfinance | — |
| 重大新闻 | SerpAPI (Google News) | — |
| 自然灾害 | USGS earthquake API + GDACS RSS | — |

## 依赖

```bash
pip install yfinance curl_cffi    # 均已安装于 ~/.hermes/hermes-agent/venv/
```

### 数据质量门禁 ⚠️

**source_health 检测逻辑：**
- yfinance → 检测美股指数 ≥2 只有效才标记 `ok`，否则 `failed`
- SerpAPI → 检测返回列表是否含 `{"error": "..."}` 或为空；key 未配时标记 `failed`
- USGS/GDACS → 捕获异常，降级标记

**yfinance 不可用时的降级行为**：如果美股指数/VIX/美债三项中至少两项不可用，`monitor.py` 输出：
```json
{
  "source_health": {"yfinance": {"status": "failed"}},
  "impact": {
    "status": "insufficient_data",
    "summary": "关键市场数据不足：美股指数/VIX/美债不可用，禁止输出方向性A股判断"
  }
}
```
此时不会生成任何"利好/利空"方向性结论，避免在数据缺失时误导。

- **非实时**：yfinance 数据有 15-20 分钟延迟，非 tick 级数据
- **盘前扫描的数据是隔夜美股收盘价**，非美股实时价（美股在北京时间 04:00 收盘）
- **新闻模块默认启用**，SerpAPI key 已配置于 `~/.hermes/.env`
- **自然灾害监控** 使用免费 API（USGS + GDACS），无需额外配置
- **非交易日不发 cron**（schedule 用 `1-5` 限定工作日）
- **Clash Verge TUN 模式**：yfinance 走 Yahoo Finance API，不走被劫持的 eastmoney 域名，不受影响

## 文件结构

```
global-market-monitor/
├── SKILL.md
├── scripts/
│   └── monitor.py               # 数据采集 + 14项影响评估引擎（~750行）
│                                 #   含：yfinance采集、自然灾害检测(USGS+GDACS)、
│                                 #   新闻关键词扫描、ETF/全球指数联动分析
├── references/
│   └── impact_mapping.md         # 全球事件→A股板块 11维映射表
└── cache/                        # 本地缓存（运行时自动创建）
```

## 相关技能

- `stock-triage` — A股编排中枢（全球信号升级入口）
- `stock-analyst` — 技术分析引擎
- `hot-money-tactics` — 游资情绪
- `news-to-sector` — 资讯→板块分析
- `serenity-investment-research` — 深度投研
- `a-stock-commands` — 快捷指令（新增 /global 指令）
