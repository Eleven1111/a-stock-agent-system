---
name: stock-triage
description: >-
  A股全栈分析编排器。接收来自 cron job 的信号或用户指令，自动判断是否升级到深度分析，
  通过四维打分引擎(four_dim_scorer.py)自动评分，支持港A联动监控、资讯触发检测、
  Serenity报告飞书存档。打板范式下接入 daban-stock-picker 候选池，策略研究接入
  chanlun-backtest 离线闸门。Kanban 分发给 stock-data / stock-analyst / hotmoney-worker /
  serenity-worker 等 profile，输出 S/A/B/C 分级的买卖建议报告。
version: 1.1.0
author: Luna
metadata:
  hermes:
    tags: [A股, 编排, 多agent, 分析, 投资]
    category: finance
    profiles:
      stock-data: 数据采集（a-stock-data + a-stock-daily-report）
      stock-analyst: 技术分析（stock-analyst + a-stock-data）
      hotmoney-worker: 游资情绪（hot-money-tactics + news-to-sector）
      serenity-worker: 深度投研（serenity-investment-research / deepseek-v4-pro）
      daban-picker: 打板候选池（daban-stock-picker + hot-money-tactics）
      strategy-research: 离线研究闸门（chanlun-backtest）
      stock-triage: 编排中枢（本 profile）
---

# A股全栈分析编排器

这是一个**决策中枢**，不会自己执行分析，而是判断 → 分发 → 汇总。
它相当于整个 A股 Agent 系统的大脑。

选股入口是动态全市场漏斗：15:02 由 `hot-money-tactics/analyze.py --cache-only` 缓存当日涨停梯队，15:05 由 `candidate_discovery.py` 生成约 200 只观察池，打板和趋势排序独立计算；次日 09:25 由 `auction_collector.py` 收敛至 20 只，09:35 由 `open_confirmation.py` 最终保留不超过 5 只。梯队缓存必须通过 `ladder_asof` 新鲜度门禁，过期时回退 neutral；开盘报价触发高度板/梯队退潮时，禁止新开打板仓并执行 `top_n_limit`。常规体检走 `four_dim_scorer.py`；游资打板候选走 `hot-money-tactics` 判断情绪和题材，再交给 `daban-stock-picker` 做机械候选过滤、六问否决和可成交性闸门。`chanlun-backtest` 只作为离线研究验证层，不作为实时买入信号源。

## 核心逻辑

```
信号输入 → 信号评分 → 决策：
  ├─ ≥8分 (S级) → 启动完整 4-worker 深度分析 + Serenity + 推送
  ├─ 6-7分 (A级) → 3-worker 技术+情绪+催化 + 每日简报
  ├─ 4-5分 (B级) → 单 worker 快扫 + 加入观察池
  └─ <4分 → 记录信号，不执行
```

## 信号检测规则

### 自动升级信号（触发 ≥ 后自动启动深度分析）

| 信号类型 | 检测条件 | 分值 | 优先级 |
|---------|---------|------|--------|
| 板块爆发 | 板块热度 > 10 且 涨停 ≥ 3家 | 7 | 高 |
| 连板加速 | 连板 ≥ 3 且 封板资金 ≥ 1亿 | 8 | 最高 |
| 资金异动 | 封板资金 ≥ 5亿（首板） | 6 | 中 |
| 用户标的技术触发 | 用户关注列表 + 技术金叉/突破 | 9 | 最高 |
| 用户标的超跌 | 用户关注列表 + 跌超 15% | 7 | 高 |
| 政策催化 | 政策/资讯命中跟踪板块 | 10 | 最高 |
| 板块轮动 | 资金净流入/流出 > 50亿切换 | 6 | 中 |
| 北向持续加仓 | 北向资金连续3日加仓 TOP5 | 5 | 中 |
| 异常放量 | 换手 > 20% + 涨 > 5% | 4 | 低 |
| 研报密集 | 3家以上券商同日覆盖 | 5 | 中 |

### 🌍 全球市场信号（新增，来自 global-market-monitor）

| 信号类型 | 检测条件 | 分值 | 优先级 | 影响板块 |
|---------|---------|------|--------|---------|
| 全球恐慌 | VIX ≥ 30 | 8 | 最高 | 全市场（外资流出，降低仓位） |
| 美股科技暴跌 | 纳斯达克跌幅 ≥ 2% | 7 | 高 | AI算力、半导体、消费电子 |
| 美股全面暴跌 | 标普500跌幅 ≥ 2% | 7 | 高 | 全市场（跟跌风险） |
| 中概股集体崩跌 | ≥3只ADR跌>5% | 6 | 中 | 互联网、电商、新能源车 |
| 关键科技股异动 | NVDA/AAPL涨跌>5% | 5 | 中 | 对应A股产业链 |
| 原油暴涨 | WTI +5% | 6 | 中 | 石油/石化利好，航空/交运利空 |
| 人民币异动 | USD/CNY波动>1% | 5 | 中 | 外贸/家电（贬利好），航空/造纸（贬利空） |
| 美债异动 | 10Y变动>10bp | 5 | 中 | 科技/成长（↑利空），银行（↑利好） |
| 地缘政治升级 | 重大冲突/制裁 | 10 | 最高 | 军工/黄金利好，相关产业链 |

全球信号通过 `global-market-monitor/scripts/monitor.py` 的 `assess_impact()` 自动检测，
盘前（08:15）和晚间（22:30）两次 cron 扫描输出后，作为 Triage 的附加输入。

### 用户指令（直接触发）

| 指令 | 语法 | 行为 |
|------|------|------|
| 深度分析 | `/deep 002156` | 启动 4-worker 全链 → S级报告 |
| 快速扫板块 | `/scan 军工` | stock-analyst 板块扫 + 识别候选 |
| 设置提醒 | `/alert 600011 止损8.0` | 添加到监控 + 价格触发 |
| 周报 | `/report 封测 本周` | 汇总该板块本周表现 + 展望 |
| 对比 | `/compare 通富微电 长电科技 华天科技` | 3只横向对比 |
| 紧急推送 | `/push` | 立即推送当前待发的所有报告 |
| 打板候选 | `/daban candidates.json` | 运行打板候选池闸门，输出可执行/否决原因 |
| 策略研究闸门 | `/chanlun research_state.json` | 检查缠论/打板回测是否满足样本外证据标准 |

## 输出格式

### S级标的报告模板

```markdown
🏆 **S级深度分析：{股票名}({代码})**
**评分：{X.X}/10 | 等级：S | 建议：🟢🟢🟢 {买入/卖出}**

**买入区间：** {价} - {价}
**止损位：** {价} （跌破 {百分比}%）
**目标位：** {价} （{天数}天 / 涨幅{百分比}%）
**仓位：** {百分比}%

---

## 四维评分卡
| 维度 | 评分 | 关键发现 |
|------|------|---------|
| 技术面 (30%) | {X}/10 | ... |
| 情绪面 (25%) | {X}/10 | ... |
| 催化面 (25%) | {X}/10 | ... |
| 深度面 (20%) | {X}/10 | ... |
| **加权总分** | **{X.X}** | |

## 供应链位置
{serenity-worker 输出}

## 财务快照
| 指标 | 当前值 | 行业均值 | 判断 |
...

## 催化剂日历
...

## 风险清单
...

## 操作建议
明确买入价/止损价/目标价/持有周期
```

## Kanban 调用模式

```python
# 场景：S级信号触发 → 启动全链分析

# Step 1: 创建 3 个并行分析任务
t1 = kanban_create(
    title="技术分析: {code} {name}",
    assignee="stock-analyst",
    body="用 stock-analyst 分析 {code} {name}。获取日线/周线技术指标，输出评分、支撑/阻力位、止损建议。"
)

t2 = kanban_create(
    title="游资情绪: {code} {name}",
    assignee="hotmoney-worker",
    body="用 hot-money-tactics 分析 {code} 所在板块的热度、连板梯队、封板质量、资金流向。输出板块情绪周期判断。"
)

t3 = kanban_create(
    title="催化分析: {code} {name}",
    assignee="hotmoney-worker",
    body="用 news-to-sector 搜索最新资讯/政策/研报催化。判断催化剂新鲜度和持续周期。"
)

# Step 2: 评分 + 深度研究（等上面 3 个完成）
t4 = kanban_create(
    title="四维评分: {code} {name}",
    assignee="stock-analyst",
    parents=[t1, t2, t3],
    body="综合技术面/情绪面/催化面分析结果，给出四维评分。若总分 ≥ 6，标注'建议升级深度研究'。"
)

# Step 3: Serenity 深度研究（仅 S/A 级）
t5 = kanban_create(
    title="🎓 Serenity深度: {code} {name}",
    assignee="serenity-worker",
    parents=[t4],
    body="启动 serenity-investment-research 深度分析。包含：供应链位置、财务拆解、估值赔率、熊市审计、完整报告。"
)

# Step 4: 汇总报告
t6 = kanban_create(
    title="汇总报告: {code} {name}",
    assignee="stock-analyst",
    parents=[t5],
    body="合并所有分析结果，输出 S/A/B/C 分级 + 买入区间/止损/目标/仓位建议，按模板格式。"
)
```

## 配置信息

- **Kanban dispatcher**: 运行在 default gateway，间隔 60s
- **Worker 模型**: stock-data/analyst/hotmoney → deepseek-v4-flash (便宜), serenity → deepseek/deepseek-v4-pro (深度)
- **用户关注标的**: 华能国际(600011)、通富微电(002156)、长电科技(600584)、华天科技(002185)、深科技(000021)、太极实业(600667)
- **跟踪板块**: 封测、高温主题(电力/电网/空调)、AI算力、军工航天、煤炭

### ⚠️ Cron 上下文隔离架构（2026-06-09 强制推行）

所有 A股 cron 任务必须隔离运行，不得在主线用户对话或 cron 主 agent 上下文里直接堆数据采集和重计算：

```
┌──────────────┐     runner / delegate     ┌──────────────────┐
│  Cron Agent  │ ────────────────────────→ │  Isolated Worker  │
│  (编排+汇总)  │                           │  (数据采集+分析)   │
│              │ ←──────────────────────── │                  │
│  只读artifact │     artifact + 摘要        │  可多组并行       │
└──────────────┘                           └──────────────────┘
```

**规则：**
1. **脚本型 cron** → manifest 的 `command` 必须走 `python scripts/hermes_job_runner.py <job-id>`；真实业务命令放 `run.command`
2. **Agent prompt 型 cron** → 必须 `delegate_task(...)` 给 subagent；主 agent 只编排、汇总、压缩输出
3. **artifact 优先** → 每次运行必须写 `$HERMES_HOME/cron/output/{job_id}/{run_id}.json`，下游只通过 `context_from` 读 artifact 摘要
4. **主 agent 只做**：编排 → 汇总 artifact → 输出（压缩到一屏以内）
5. **无信号则静默** — 盘中异动、资讯监控等 cron 在无触发时 `[SILENT]`
6. **并行** — 可拆分的多组采集（如同时拉多板块行情）用 `tasks` 数组或多个 isolated job 并行
7. **输出压缩** — 每个 cron 的输出控制在 `max_output_chars` 内，例行任务 `deliver=local`

**为什么必须这么做：**
- 单一 agent 同时处理数据采集→分析→输出，任务一多就乱套（上下文膨胀、工具调用互相干扰）
- runner/subagent 隔离上下文，每个 worker 只做一件事，结果落入 artifact
- 主 agent 的上下文不会被中间数据撑爆，始终专注在编排和输出

## Cron 流水线时间表

> 设计原则详见 `references/cron-scheduling-principles.md`：时间分散、上下文链路、静默式vs定时式、推送分层、数据采集模式。

推送层级：🔴紧急 > 🟡重要 > 🟢常规 > ⚪数据

| 时间 | 任务 | 层级 | 来源 |
|------|------|------|------|
| 08:00 Mon | 📅 事件日历提醒 | ⚪ | event_calendar.py |
| 08:15 | 🌍 全球市场盘前扫描 | 🟢 | global-market-monitor |
| 08:30 | BuilderPulse → 飞书 | 🟢 | — |
| 08:55 | PulseEngine 日报 (已调至07:00) | 🟢 | — |
| 09:00 | 📡 资讯监控(政策/产业/地缘) | 🟡 | SerpAPI 4组关键词 |
| **09:15-09:24** | **集合竞价快照采集** | **⚪** | **auction_collector.py** |
| 09:00-15:00 | ⚡ 盘中异动（每5分钟） | 🔴 | intraday_monitor.py |
| **09:25** | **⚡ 集合竞价收口+候选池** | **🟡** | **daban-stock-picker + hot-money-tactics** |
| **09:35** | **⚡ 开盘确认+上车判定** | **🟡** | **daban-stock-picker + stock-analyst** |
| 09:45 | 🇭🇰 港A联动 | 🟢 | hk_a_linkage.py |
| 10:00 | 高温主题开盘跟踪 | 🟢 | stock-analyst |
| 10:30 | 💰 资金流向 | 🟡 | capital_flow_monitor.py |
| 11:00 | 📡 资讯监控 | 🟡 | SerpAPI |
| 11:35 | 午盘热门板块复盘 | 🟢 | stock-analyst |
| 11:40 | 🧠 午盘Triage快速扫描 | 🟡 | stock-triage |
| 13:00 | 📡 资讯监控 | 🟡 | SerpAPI |
| 13:45 | 🇭🇰 港A联动 | 🟢 | hk_a_linkage.py |
| 14:00 | 📡 资讯监控 | 🟡 | SerpAPI |
| 14:30 | 💰 资金流向 | 🟡 | capital_flow_monitor.py |
| 14:45 | 🇭🇰 港A联动 | 🟢 | hk_a_linkage.py |
| 15:02 | 收盘涨停梯队缓存 | ⚪ | hot-money-tactics/analyze.py --cache-only |
| 15:05 | 全市场动态候选发现（约200只） | ⚪ | candidate_discovery.py |
| 15:18 | 📊 动态前20只四维复核 | 🟡 | batch_four_dim_scorer.py |
| 15:25 | 🛡️ 持仓风控检查 | 🔴 | portfolio_manager.py |
| 15:15 | 收板热门板块复盘 | 🟢 | stock-analyst |
| 15:20 | 📡 资讯监控 | 🟡 | SerpAPI |
| 15:35 | 🧠 收盘Triage→Kanban派发 | 🟡 | stock-triage |
| 20:00 | 📡 资讯监控 | 🟡 | SerpAPI |
| 22:00 | 📡 资讯监控 | 🟡 | SerpAPI |
| 22:30 | 🌙 全球市场晚间扫描 | 🟢 | global-market-monitor |
| Sat 10:00 | 🏛️ 机构行为周报 | ⚪ | institution_tracker.py |
| Sun 10:00 | 📈 信号胜率统计周报 | ⚪ | performance_tracker.py |

收盘 Triage（15:35）的 `context_from` 链：[候选发现(15:05), 四维复核(15:18), 持仓风控(15:25), 资金流向(14:30)] → 只读取上游 artifact 摘要，不读取主线对话历史。

**推送分层策略：**
- 🔴 紧急：止损触发、跌停、地缘冲突 — 立即推送
- 🟡 重要：S/A级信号、北向异动、政策催化 — 推送摘要
- 🟢 常规：每日复盘、盘前扫描 — 静默存档，主动查看
- ⚪ 数据：周报、无触发监控 — 不推送，存档备查

## 可执行脚本

| 脚本 | 用途 |
|------|------|
| `candidate_discovery.py` | 上交所/深交所全市场列表 + 腾讯批量行情/K线，基础过滤后分别计算打板与趋势排序（打板分叠加游资因子：连板在册/率先封板/封单比/板块集群），输出附情绪温度计裁决（五档仓位/top_n约束），生成动态观察池并维护候选生命周期 |
| `../../common/market_temperature.py` | 情绪温度计：高度板×连板晋级率→冰点/修复/发酵/加速/极热，输出打板准入+仓位倍率+退潮硬信号 |
| `batch_four_dim_scorer.py` | 默认读取动态观察池前20只做四维复核；仅在人工调试时使用 `--targets` |
| `four_dim_scorer.py` | 四维打分引擎：技术(30%)×情绪(25%)×催化(25%)×深度(20%)→ S/A/B/C/D；深度面回流 Serenity、技术面接缠论(过闸才计权)、情绪面接连板梯队/板块赚钱效应/资金流(signal_context)、催化面分级×新鲜度衰减、出分后叠大盘 overlay(market_context)；支持 `--timeframe 60/30` 短线入场 |
| `capital_flow_monitor.py` | 资金流向：北向资金 + 主力/散户净流 + 板块资金（东财API，需NO_PROXY）；`--cache` 落情绪上下文供四维消费 |
| `portfolio_manager.py` | 持仓风控：`--add`开仓、`--close`清仓、`--check`止损止盈/仓位集中度 |
| `intraday_monitor.py` | 盘中异动：涨跌停/放量>10%/急涨急跌>5%（5分钟静默式，无触发不输出） |
| `hk_a_linkage.py` | 港A联动：AH溢价率 + 恒生vs上证背离 + 港股通权重异动检测 |
| `institution_tracker.py` | 机构行为：调研/研报/增减持（东财数据中心+SerpAPI） |
| `event_calendar.py` | 事件日历：限售解禁/分红/政策窗口（东财+固定日期库） |
| `performance_tracker.py` | 胜率统计：`--record`记录信号→自动跟踪表现→分S/A/B/C/策略统计命中率；`--gate`按实盘期望值淘汰负期望策略(写strategy_registry) |
| `recommendation_audit.py` | 推荐审计档案：买/卖/加/减建议写入 `recommendations.json`，支持查询、结果更新、赔率/凯利仓位测算 |
| `monitor_manager.py` | 动态监控订阅：股票/板块/主题的添加、取消和查询；取消写入持久墓碑 |
| `serenity_to_feishu.py` | 飞书存档：接收 markdown 报告 → lark-cli docs +create → 本地双份存档 |
| `../daban-stock-picker/scripts/daban_candidate_api.py` | 打板候选池：主板10cm首板回封/二板弱转强 → 六问否决 + 可成交性 |
| `../chanlun-backtest/scripts/research_gate.py` | 离线研究闸门：IS/OOS、成本、对照组、统计检验完整性检查；`--register`登记结论到策略注册表 |
| `../chanlun-backtest/scripts/chan_structure.py` | 缠论结构信号：分型/笔/中枢/三买三卖/背驰 → JSON（过闸才计权） |
| `../../common/strategy_registry.py` | 策略闸门+门控裁决：缠论信号是否计权、负期望策略是否停用 |
| `../../common/deep_research_cache.py` | Serenity 深研缓存读写：回流四维深度面（深研一次、日评复用） |

快速命令：

```bash
PY=~/.hermes/hermes-agent/venv/bin/python3
SDIR=~/.hermes/skills/stock-triage/scripts

# 评分 — 支持 --timeframe 60/30 短线入场判断
$PY $SDIR/four_dim_scorer.py 600011 华能国际
$PY $SDIR/four_dim_scorer.py 600011 华能国际 --timeframe 60

# 监控 — 均支持 --json 输出
$PY $SDIR/intraday_monitor.py          # 盘中异动（无触发静默）
$PY $SDIR/capital_flow_monitor.py      # 资金流向
$PY $SDIR/hk_a_linkage.py              # 港A联动

# 持仓
$PY $SDIR/portfolio_manager.py --add 600011 华能国际 9.10 --shares 2000
$PY $SDIR/portfolio_manager.py --check
$PY $SDIR/portfolio_manager.py --close 600011 8.50

# 追踪
$PY $SDIR/institution_tracker.py       # 机构行为
$PY $SDIR/event_calendar.py            # 事件日历
$PY $SDIR/performance_tracker.py --record 600011 华能国际 A 9.10
$PY $SDIR/performance_tracker.py --record 002156 通富微电 S 11.00 --score 9.7 --strategy-id daban:first_board_reseal
$PY $SDIR/performance_tracker.py       # 查看胜率
$PY $SDIR/recommendation_audit.py --record 002156 通富微电 buy "10.80-11.00" "半导体主线早盘回封" --strategy-id daban:first_board_reseal
$PY $SDIR/recommendation_audit.py --code 002156

# 打板候选池 / 策略研究闸门
$PY ~/.hermes/skills/daban-stock-picker/scripts/daban_candidate_api.py --example --json
$PY ~/.hermes/skills/chanlun-backtest/scripts/research_gate.py --example --json

# 存档 — 从 stdin 读取 markdown
echo "# 深度报告..." | $PY $SDIR/serenity_to_feishu.py "通富微电"
```

## 相关技能

- `stock-analyst` — 技术分析引擎（被 Triage 分派的主要 worker）
- `hot-money-tactics` — 游资情绪（hotmoney-worker 的 skill）
- `news-to-sector` — 资讯驱动催化分析（hotmoney-worker 的 skill）
- `serenity-investment-research` — Serenity 深度投研（serenity-worker 的 skill）
- `global-market-monitor` — 🆕 全球市场监控（外围输入，信号升级入口）
- `daban-stock-picker` — 打板候选池（首板回封/二板弱转强的机械过滤和六问否决）
- `chanlun-backtest` — 离线研究闸门（策略上线前的统计证据检查）
- `a-stock-commands` — 快捷指令（/deep /scan /alert /report /compare /global）

## 8个拓展场景

已在 `references/8-expansion-scenarios.md` 中定义了 8 个可插拔的拓展场景，优先级 P0→P3：

| 优先级 | 场景 | 关键数据 |
|--------|------|---------|
| P0 | 连板追击 | hot-money-tactics 连板≥3 + 封板资金 |
| P0 | 技术突破 | stock-analyst screener 突破阻力+放量 |
| P1 | 板块轮动 | --rotation 退潮检测 + 新热点识别 |
| P1 | 政策驱动 | 资讯监控触发词 + news-to-sector 映射 |
| P2 | 抄底策略 | RSI<25 + 跌破布林+缩量 |
| P2 | 北向跟踪 | 北向资金端点（需NO_PROXY） |
| P2 | 事件驱动 | 财报/产品发布/行业展会日历 |
| P3 | 估值修复 | PE历史分位 + 业绩增长 + 横盘确认 |

## ⚠️ 构建原则

**设计即构建**：在 SKILL.md 中描述的任何模块（脚本、cron job、集成管道），必须在同一次交付中建成可运行的实现。不允许出现"设计完成但未实现"的半成品——这会导致用户追问和重复工作。本 skill 中的所有脚本、cron 和管道均已完成并验证。

### ⚠️ 持仓数据准确性铁律

持仓信息在 persistent memory 中可能是过时或错误的。**做任何涉及持仓的分析/推荐前：**

1. 先跑 `portfolio_manager.py --balance --json` 获取当前持仓+现金
2. 只有它返回的数据才是权威源
3. memory 中的持仓数据仅作话题线索，不可直接用于计算仓位/盈亏

曾犯错误（2026-06-05）：persistent memory 中残留了一条"证券ETF 512880 3100份"的虚假持仓记录（上一轮 session 的笔误），导致用户在"清仓"时多算了 3,200 元现金。持仓来源只有 portfolio_manager。memory 里的持仓记录只是笔记，不是账本。

## 项目维护

### GitHub → 本地同步

当用户要求"同步GitHub更新到本地"时，执行 `references/repo-sync.md` 中的完整流程。核心步骤：

1. `git fetch origin`（设 timeout=120 应对代理超时）
2. `git pull origin main --ff-only`
3. 复制脚本到 `~/.hermes/skills/stock-triage/scripts/`
4. 复制 `common/` 共享模块（最易遗漏）
5. 验证 `portfolio_manager.py --check`
6. 如有数据兼容问题（现金对账/历史文件格式），按坑位修复
