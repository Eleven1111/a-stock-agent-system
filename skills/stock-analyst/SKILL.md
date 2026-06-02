---
name: stock-analyst
description: "股票技术分析工具。基于多数据源（腾讯/新浪/BaoStock/SerpAPI）的本地缓存+技术指标分析系统，支持个股分析、板块批量扫描、涨停板监控、多因子评分、基本面分析、条件筛选、K线图、新闻驱动分析。纯numpy计算，无需talib。支持A股全量分析及港股基础行情+新闻分析。"
version: 3.0.0
author: Luna
metadata:
  hermes:
    tags: [A股, 技术分析, 选股, 板块扫描, MACD, RSI, KDJ, 基本面, 新闻, 条件筛选]
    category: finance
---

# Stock Analyst — 股票分析工具 (v3)

基于多数据源汇聚 + 本地SQLite缓存 + 纯numpy技术指标计算的分析系统。覆盖技术面、基本面、新闻情绪三个维度。

## 数据架构

```
                     ┌──────────────┐
                     │   SQLite缓存  │
                     │ daily_kline  │
                     │ realtime     │
                     └──────┬───────┘
                            ↑
  数据源链（有降级）：腾讯 ifzq → 新浪 → BaoStock（支持日/周/月）
  实时行情：腾讯 qt.gtimg.cn
  资金流向：push2his.eastmoney.com（需 NO_PROXY 绕过 Clash，见 references/clash_proxy_bypass.md）
  涨停板：AkShare stock_zt_pool_em
  基本面：BaoStock（利润表、杜邦分析、ROE、营收同比）
- **SerpAPI**: Google News + Trends（3 key 轮询，见 `references/serpapi_key_rotation.md`）
- **港股行情**: 腾讯 `qt.gtimg.cn/q=hkXXXXX`，仅实时行情+新闻（见 `references/hk-stock-capabilities.md`）
```

## 触发场景

- "帮我分析一下600011"、"分析华能国际"
- "扫一下火电板块"、"高温主题现在什么位置"
- "今天涨停板有什么"
- "大盘现在怎么样"
- "通富微电有什么新闻"
- "查一下封测板块的搜索热度"
- "全市场RSI超卖的股票有哪些"

## 全市场扫描工作流

详见 `references/sector-scan-workflow.md`，五步法：
1. `sector_scan.py` 遍历所有行业板块（⚠️ 必须全量，不得只扫预设板块）
2. `hot-money-tactics --rotation` 板块轮动追踪
3. `analyst.py` 单股/板块批量深度分析
4. 三因子交叉判断（热度+技术面+催化）→ S/A/B/C 分级+买卖点
5. **Triage 升级 → Kanban 派发 → Serenity 深度投研**（见下方「多Agent Kanban编排系统」）

最后输出的报告必须包含具体买入区间、止损位、目标位、持有周期和仓位建议。

## 使用

```bash
PY=~/.hermes/hermes-agent/venv/bin/python3
ANALYST=~/.hermes/skills/stock-analyst/analyst.py

# === 技术面 ===
$PY $ANALYST analyze 600011 华能国际        # 单股技术分析
$PY $ANALYST weekly 600900 长江电力          # 周线级别分析
$PY $ANALYST screen 封测                    # 预设板块批量扫描
$PY $ANALYST realtime 600011,600027         # 实时行情
$PY $ANALYST chart 600011 华能国际 30       # K线图
$PY $ANALYST zt                              # 今日涨停板
$PY $ANALYST index                           # 大盘指数

# === 基本面 ===
$PY $ANALYST fundamental 002156 通富微电    # 基本面（ROE/营收/净利）
$PY $ANALYST compare 封测                   # 板块横向对比（ROE+营收+技术评分）

# === 全市场 ===
$PY $ANALYST screener                       # 列出可用筛选条件
$PY $ANALYST screener "rsi<30 AND volume_ratio>1.2"  # 条件筛选引擎
$PY $ANALYST backtest 002156                # 回测评分系统

# === 新闻 ===
$PY $ANALYST news 002156 通富微电           # 个股新闻（含间接资金流向信号）
$PY $ANALYST news sector 电力               # 板块新闻
$PY $ANALYST news market                    # 大盘新闻
$PY $ANALYST news trend 封测                # 搜索热度趋势
```

## 预设板块（14个）

| 板块 | 核心标的 |
|------|---------|
| 火电 | 华能国际、华电国际、大唐发电、浙能电力、国投电力 |
| 水电 | 长江电力、华能水电、国投电力、川投能源、桂冠电力 |
| 电网 | 许继电气、国电南瑞、特变电工、三星医疗、国网英大 |
| 空调 | 格力电器、美的集团 |
| 高温主题 | 火电+水电+电网+空调合并 |
| 煤炭 | 山西焦煤、淮北矿业、平煤股份、山煤国际、晋控煤业、兖矿能源 |
| 封测 | 通富微电、长电科技、华天科技、深科技、太极实业 |
| 消费电子 | 立讯精密、工业富联、歌尔股份、蓝思科技、领益智造 |
| 半导体 | 中芯国际、韦尔股份、北方华创、圣邦股份、中微公司 |
| AI算力 | 中际旭创、工业富联、海光信息、寒武纪、中科曙光 |
| 军工航天 | 中国卫通、中国卫星、航天电子、中航重机、航天发展 |
| 新能源 | 阳光电源、隆基绿能、宁德时代、晶澳科技、天合光能 |
| 券商金融 | 中信证券、华泰证券、招商证券、东方财富、国泰君安 |
| 汽车 | 上汽集团、长安汽车、广汽集团、比亚迪、北汽蓝谷 |

## 技术指标

- **趋势**: MA5/10/20/60、多头/空头排列、金叉/死叉
- **动量**: MACD(DIF/DEA/柱)、RSI(6/14)、KDJ(K/D/J)
- **波动**: 布林带(20,2)、突破/跌破判断
- **量能**: 成交量比（相对5日均量）

## 评分体系

综合评分范围 -10 ~ +10：

| 评分 | 评级 | 条件 |
|------|------|------|
| +4以上 | 强烈买入 🟢🟢 | 多指标共振看多 |
| +2 ~ +3 | 买入 🟢 | 技术面偏多 |
| 0 ~ +1 | 观望 🌤 | 中性偏多 |
| -1 ~ -2 | 谨慎 🔶 | 技术面偏空或超买 |
| -2以下 | 卖出/回避 🔴 | 多指标警告，强制触发 |

## ⚠️ 客观性铁律（代码层强制执行）

1. **级别不可越级提拔** — 板块热度分14+→S, 10-14→A, 6-10→B, <6→观察
2. **趋势空头锁评分上限** — MA5<MA10<MA20<MA60时，评分上限锁定为-0.5，不可能给买入
3. **价格<MA20时超卖不视为买入** — 跌破布林下轨+价格在MA20以下时，信号改为"加速下跌中，等待企稳"
4. **评分<+2不给买入评级** — 只有评分≥+2才能出现🟢
5. **评分≤-2强制卖出** — 自动触发🔴
6. **用户历史关注标的必须标注** — 防止对话历史污染分析结果

**曾犯错误（已修复）：**
- ❌ 中国卫通趋势空头+评分+1.5 不应给布局建议
- ❌ 中科曙光跌破布林下轨+价格<MA20 不应给买入

## Cron 执行注意事项

详见 `references/cron-pitfalls.md`：

1. **terminal 命令在 cron 下会触发审批锁** — 所有cron数据采集必须用 `execute_code` + Python `urllib`，禁止用 `terminal`
2. **DeepSeek 冷启动** — 首次API调用可能耗时200-300s，设置timeout=300+
3. **Prompt 必须自包含** — cron无对话上下文，所有信息必须在prompt内
4. **定期跟踪型 job 用 `repeat: forever`，事件型 job 用 `repeat: once`**
5. **时间分散错开** — 避免整点，避免同分钟多job并发。新建job前检查现有时间表

## SerpAPI 多 Key 轮询

详见 `references/serpapi_key_rotation.md`。

**为什么需要多 key：** SerpAPI 按 key 独立计费，免费额度有限。高温主题跟踪、市场新闻轮询等多个 cron job 同时调用时，单 key 可能快速耗尽。

**当前配置：** 3 个 key 循环有序轮询，每次 `_serpapi_request()` 自动取下一个。

## 数据源限制

- **东财 push2/push2his** — Clash Verge DNS 劫持导致 502，已通过在 `.env` 设 `NO_PROXY` 绕过。详见 `references/clash_proxy_bypass.md`
- **BaoStock 基本面** — 无需注册/API key
- **腾讯 ifzq K线 / qt.gtimg.cn 实时行情** — 全天候可用 ✓
- **SerpAPI 新闻** — 3 key 轮询，按量使用
- **资金流向** — 现在 `akshare stock_individual_fund_flow()` 可用（需 NO_PROXY），同时新闻提取做间接信号补充

## 多Agent Kanban编排系统

stock-analyst 是 A股全栈分析 Agent 系统的三个技术 worker 之一。整个系统通过 Kanban 板编排：

```
Cron 流水线 → Triage 编排器 → Kanban 板 → Worker Profiles → 报告
                    │
         ┌──────────┼──────────┐
         ↓          ↓          ↓
  stock-analyst  hotmoney  serenity-worker
  (技术面)      (情绪/催化)  (深度投研)
         └──────────┼──────────┘
                    ↓
              四维评分 → S/A/B/C
                    ↑
         global-market-monitor
         (外围输入: VIX/美股/期货/汇率)
```

### Worker Profiles（Kanban profile — 用 `hermes -p <name>` 调用）

| Profile | 模型 | 角色 | 绑定的 Skill |
|---------|------|------|-------------|
| `stock-analyst` | deepseek-v4-flash (OpenRouter) | 技术分析 | stock-analyst, a-stock-data |
| `hotmoney-worker` | deepseek-v4-flash (OpenRouter) | 游资情绪 | hot-money-tactics, news-to-sector |
| `serenity-worker` | deepseek/deepseek-v4-pro (OpenRouter) | 深度投研 | serenity-investment-research, stock-analyst |
| `stock-triage` | deepseek-v4-flash (OpenRouter) | 编排中枢 | 全部 + stock-triage skill |

### 快捷指令（Discord 中直接输入）

| 指令 | 作用 | 触发路径 |
|------|------|---------|
| `/deep 002156` | 深度分析 | Triage → 4-worker全链 → Serenity |
| `/scan 军工` | 板块扫描 | stock-analyst 快速扫描 |
| `/alert 600011 止损8.0` | 价格提醒 | 写入提醒数据库，每5分自动检查 |
| `/report 封测 本周` | 汇总报告 | stock-analyst + hotmoney |
| `/compare A B C` | 横向对比 | stock-analyst compare 模式 |
| `/global` | 全球扫描 | global-market-monitor → news-to-sector |

### 信号升级规则

当 Cron 流水线中的分析结果被 Triage 编排器拿到后，自动评分：

| 评分 | 等级 | 行动 |
|------|------|------|
| ≥ 8 | S | 4-worker全链 + Serenity深度投研 + 推送 |
| 6-7 | A | 3-worker（技术+情绪+催化），不加 Serenity |
| 4-5 | B | 单 worker 快扫 + 观察池 |
| < 4 | — | 记录信号，不执行 |

S级信号触发 Serenity 的完整供应链分析、财务拆解、估值赔率、熊市审计。

### 全局配置

- **所有 A 股 Worker profile 默认 provider**: OpenRouter（`OPENROUTER_API_KEY`）
- **轻量级 worker**（stock-analyst, hotmoney-worker, stock-triage）: `deepseek-v4-flash`（便宜）
- **深度 worker**（serenity-worker）: `deepseek/deepseek-v4-pro`（贵，计费分开）
- **官方 DeepSeek API**（非 OpenRouter）: 通过修改 `config.yaml` 的 `model.provider` 为 `deepseek` 切换

## Skill Hub 生态现状

当用户问"有没有别的 skill 可以实现"，首选检查 Skill Hub 是否真有替代方案。以下是已知事实（2026-06-02 实测）：

- **Hub 里几乎所有的 A 股技能依赖 AkShare/eastmoney** — `a-stock-review`、`a-stock-data`、`a-stock-screener`、`stock-alpha` 等数十个 clawhub 技能，底层全走 push2.eastmoney.com。这些技能现在也能用了（NO_PROXY 绕过 Clash），但功能上不超出 stock-analyst 已有的能力。
- **GitHub repo tap 不可用** — `hermes skills tap add github:xxx` 可以注册 tap，但 GitHub 被 Clash 代理延迟/超时（connect timeout），repo 拉不下来。不去尝试安装 GitHub 来源的 A 股技能。
- **skills.sh 来源的 400+ 个 skills** — 在 skill hub 搜索时能看到它们（缓存在本地 index），但 `hermes skills inspect/install` 都需要从 skills.sh/GitHub 下载内容，Clash 下绝大多数失败。
- **唯一本地可用的 clawhub 技能** — `a-stock-data`（已安装）、`a-stock-review`（可安装）、`money-flow`（方法论知识，无数据抓取能力）、`magpie`（需本地 daemon）。
- **结论** — Clash 环境下，安装额外 A 股技能不能解决任何 stock-analyst 已有能力之外的问题。**stock-analyst + hot-money-tactics + news-to-sector + serenity-investment-research + global-market-monitor + stock-triage 编排器** 构成完整的 A 股全栈分析系统，无需安装额外 Hub 技能。

## 文件结构

```
stock-analyst/
├── SKILL.md
├── analyst.py                  # 主入口 + 预设板块组合
├── scripts/
│   ├── data_cache.py           # 数据缓存层 + 多源抓取
│   ├── tech_analysis.py        # 技术指标计算 + 分析评分（含客观性硬约束）
│   ├── screener.py             # 条件筛选引擎（全市场搜索）
│   ├── fundamentals.py         # 基本面数据（BaoStock ROE/营收/杜邦）
│   ├── chart.py                # 终端ASCII K线图
│   ├── news.py                 # SerpAPI新闻搜索 + 资金信号自动提取
│   └── sector_scan.py          # 全市场板块热度扫描（涨停聚合）
├── references/
│   ├── clash_proxy_bypass.md   # Clash DNS劫持绕过（NO_PROXY方案）
│   ├── data_sources.md         # 数据源技术细节和API字段映射
│   ├── cron-pitfalls.md        # Cron作业常见陷阱和解决方案
│   ├── fund_flow_from_news.md  # 资金流向替代方案：SerpAPI新闻提取
│   └── sector-scan-workflow.md # 全市场扫描四步法
```

## 相关技能

- `hot-money-tactics` — 游资战法、板块轮动追踪（--rotation）、涨停板情绪周期
- `news-to-sector` — 资讯驱动板块分析（产业链传导）
- `serenity-investment-research` — Serenity 风格深度投研（供应链瓶颈、财务拆解、估值赔率）— 仅 S/A 级信号触发
- `global-market-monitor` — 全球市场监控（VIX/美股/期货/汇率/中概ADR → A股板块影响评估）
- `stock-triage` — A股编排中枢（信号检测、Kanban 派发、评分升级）
- `a-stock-commands` — 快捷指令（/deep /scan /alert /report /compare /global）
