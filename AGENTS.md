# A 股全栈 Agent 系统 · AGENTS.md

> 项目宪法。任何 agent 在此项目目录下工作时，必须先读本文。

## 系统身份

这是一个 **A 股投资决策辅助系统**，不是交易机器人。它的职责是：
- 采集数据、分析信号、给出买卖建议
- **不自动下单、不操作账户、不代替人做最终决策**

## 架构

### A股执行规则硬约束

- 本系统默认市场始终是中国 A 股（SSE/SZSE），股票现货执行遵守 **T+1**。
- 当日新买入或加仓的股份，当日不得建议卖出、减仓或止损；必须输出
  `same_day_sell_allowed=false` 和最早可卖交易日。
- 盘中触发止损但股份仍被 T+1 锁定时，只能标记“风险已触发、次一交易日处置”，
  不得伪造当日可执行卖出动作。
- 每条方向性个股建议必须先通过公告、可成交性、价格计划和风险质检；未扫描公告
  只能输出关注/条件建议，不能输出无条件买入。
- 持仓、推荐、监控订阅必须使用同一运行时状态根目录。Hermes 与 OpenClaw 并用时，
  两端设置相同的 `A_STOCK_STATE_HOME`。

```
                        ┌─────────────────┐
                        │   stock-triage   │  编排中枢
                        │  (决策 + 派发)    │
                        └────────┬────────┘
                                 │
        ┌────────────┬───────────┼───────────┬────────────┬────────────┐
        ▼            ▼           ▼           ▼            ▼            ▼
   stock-analyst  hot-money  global-mkt  news-to-sector  serenity  daban-picker
   (技术分析)     (游资情绪)   (全球监控)   (催化映射)     (深度投研) (打板候选)
        │            │           │           │            │            │
        └────────────┴───────────┴───────────┴────────────┴────────────┘
                                 │
                    ┌────────────┼────────────┬────────────┬────────────┐
                    ▼            ▼            ▼            ▼            ▼
               capital_flow  portfolio    intraday   chanlun-backtest
               (资金流向)     (持仓风控)    (盘中异动)    (离线研究闸门)
```

**技能树（12 个 skill）：**

| Skill | 路径 | 角色 |
|-------|------|------|
| stock-triage | `~/.hermes/skills/stock-triage/` | 🧠 编排中枢 |
| stock-analyst | `~/.hermes/skills/stock-analyst/` | 📊 技术分析引擎 |
| hot-money-tactics | `~/.hermes/skills/hot-money-tactics/` | 🔥 游资情绪 |
| global-market-monitor | `~/.hermes/skills/global-market-monitor/` | 🌍 全球外围 |
| news-to-sector | `~/.hermes/skills/news-to-sector/` | 📡 资讯→板块映射 |
| serenity-investment-research | `~/.hermes/skills/serenity-investment-research/` | 🎓 深度投研 |
| daban-stock-picker | `~/.hermes/skills/daban-stock-picker/` | ⚡ 打板候选池 |
| chanlun-backtest | `~/.hermes/skills/chanlun-backtest/` | 🧪 离线研究闸门 |
| a-stock-data | `~/.hermes/skills/a-stock-data/` | 📦 数据源参考 |
| a-stock-daily-report | `~/.hermes/skills/a-stock-daily-report/` | 📋 每日简报 |
| a-stock-commands | `~/.hermes/skills/a-stock-commands/` | ⌨️ 快捷指令 |
| pulse-engine | 外部项目 | 📡 社会情绪（不在本仓库） |

## 数据源铁律

### ✅ 始终可用（24×7）

| 源 | 端点 | 编码 | 覆盖 |
|----|------|------|------|
| 腾讯实时 | `qt.gtimg.cn/q={market}{code}` | GBK | A股/港股实时行情 |
| 腾讯K线 | `web.ifzq.gtimg.cn/appstock/app/fqkline/get` | JSON | 日/周/月/60/30 K线 |
| 上交所股票列表 | `query.sse.com.cn/sseQuery/commonQuery.do` | JSON | 沪市A股完整证券列表 |
| 深交所股票列表 | `szse.cn/api/report/ShowReport` | XLSX/XML | 深市A股完整证券列表 |
| 新浪实时 | `hq.sinajs.cn/list={codes}` | GBK | A股实时行情 |
| yfinance | Yahoo Finance | — | 美股/全球指数/期货/VIX/汇率 |

### ⚠️ 需 Hermes Agent 环境（cron 内自动可用，终端需手动加载 .env）

| 源 | 端点 | 需要 |
|----|------|------|
| 东方财富 | `push2his.eastmoney.com` | NO_PROXY=.eastmoney.com |
| 东财数据中心 | `datacenter.eastmoney.com` | NO_PROXY=.eastmoney.com |
| SerpAPI | `serpapi.com` | SERPAPI_API_KEY |

### ❌ 不可用

| 源 | 原因 |
|----|------|
| BaoStock K线 | macOS 12 兼容性问题（备用） |
| 新浪港股 | `hq.sinajs.cn/list=hkXXXXX` 返回 Forbidden |
| 东财 `push2.eastmoney.com`（TUN直连） | Clash Verge DNS 劫持 → 198.18.x.x |

### 编码规则

- **腾讯** 返回 GBK，必须 `.decode("gbk")`
- **新浪** 返回 GBK，必须 `.decode("gbk")`
- **yfinance** 返回标准 Python 对象，UTF-8
- **东财 JSON API** 返回 UTF-8 JSON
- **写入文件/输出** 始终 UTF-8

## 绝对铁律（违反即事故）

### 1. 配置隔离
四个工具配置**绝不串改**：
```
OpenClaw → ~/.openclaw/
Claude Code → ~/.claude/
Codex → ~/.codex/
Hermes → ~/.hermes/
```
**改配置前必须确认目标工具。**

### 2. Cron 运行隔离
Hermes manifest 的 `command` **必须**走 `python scripts/hermes_job_runner.py <job-id>`，
真实业务命令放在 `run.command`，并写入 `$HERMES_HOME/cron/output/{job_id}/{run_id}.json` artifact。

如果 cron 由 Agent prompt 实现，主 cron agent 只能编排/汇总，数据采集和重计算必须委托子代理；
如果 cron 由仓库脚本实现，只能通过 `hermes_job_runner` 启动隔离子进程。

业务脚本内所有数据抓取必须用 Python `urllib`，禁止在 cron prompt 中直接使用 `terminal` 工具
（会触发安全审批锁，导致 cron 卡死）。

选股链路使用 `signal_context.lianban_ladder` 前必须校验 `ladder_asof`。15:05 候选发现只接受
当日缓存；次日开盘温度闸门只接受允许窗口内的最近交易日缓存。缺失、未来或过期上下文必须
回退 neutral，禁止继续用旧梯队影响排名、仓位或打板名额。

### 3. 全量扫描
用户说"全量"时必须真正穷尽，不能只扫预设板块。
参考：`sector_scan.py` 遍历所有行业板块。

### 4. 分析输出
当置信度为"高"或"中"时，个股分析必须包含：
- 具体买入价 / 止损位 / 目标位
- 持有周期
- S/A/B/C 分级 + 仓位建议

置信度为"低"或数据不足时，禁止输出方向性投资判断。

### 5. 网络故障处理
遇到 502 / DNS 劫持 / 连接拒绝等报错时：
- **先排查根因，修好，再汇报结果**
- 不要接连报错让用户处理
- 不要反复重试同一个失败端点（最多 2 次）

### 6. Cron 时间规则
- 不扎堆，时间分散错开
- 避免整点（如 08:30/08:55 而非 09:00）
- 新建 cron 前先 `cronjob(action='list')` 检查现有时间表

## 添加新功能的检查清单

1. **数据源是否在可用列表内？** → 不在则先验证端点
2. **是否需要新的 Python 依赖？** → 先 `pip install` 到 Hermes venv
3. **脚本是否 cron-safe？** → 只用 `urllib`，别用 `requests`（除非加载 `.env`）
4. **Manifest 是否隔离？** → `command` 走 `hermes_job_runner`，`run.command` 指向 canonical `skills/.../scripts/...`
5. **Cron 时间是否与现有冲突？** → 查 `cronjob list`，并跑 `validate_cron_manifest.py`
6. **是否需要更新 AGENTS.md？** → 数据源/铁律有变化必须更新
### 7. 所有 cron 走子代理模式（2026-06-09 定下的铁律）
所有 agent-driven cron job（`no_agent=False`）的 `enabled_toolsets` 必须包含 `delegation`，
格式固定为 `["terminal", "file", "web", "delegation"]`。

数据采集必须由 subagent 通过 `delegate_task` 执行，
main agent 只做编排+汇总输出。违反此条导致 token 浪费或上下文膨胀。

`no_agent=True` 的脚本任务（如价格提醒）例外，不需要 delegation。

### 8. Agent cron 模型选型铁律（2026-06-11 事故修复）

**事故：** DeepSeek 官方 API 在 cron 环境频繁 `ReadTimeout`（180s+ 无响应），
导致 cron worker 输出被进度标记污染、JSON 解析失败，所有 agent-driven cron 连续多日全部报错。

**排查路径：**
1. ❌ `deepseek-v4-flash` via DeepSeek 官方 API → 频繁超时 180s+
2. ❌ `deepseek-v4-flash` via OpenRouter 代理 → 同模型同推理服务，一样慢
3. ❌ `google/gemini-2.5-flash` via OpenRouter → **HTTP 403 geo-block（中国区不可用）**
4. ✅ `qwen/qwen3.6-flash` via OpenRouter → 首响应 3.4s，稳定通过

**铁律：**
- Agent-driven cron **禁止使用 DeepSeek 官方 API**（cron 环境不可靠）
- **禁止使用 Google Gemini 系列** via OpenRouter（中国区 geo-block）
- **统一使用 `qwen/qwen3.6-flash` via OpenRouter**（阿里出品，中国区无障碍，免费稳定）
- 其他国产可用备选：`minimax/minimax-m1`
- 切换模型后必须在 agent.log 确认 API 首轮调用成功（日志关键字：`API call #1`）

7. **输出是否控制在 Discord 一屏内？** → 配置 `max_output_chars`，例行任务 `deliver=local`
8. **无信号时是否静默？** → 高频 cron（盘中异动、资讯监控）必须静默

## 脚本规范

### 文件位置
```
~/.hermes/skills/{skill-name}/
├── SKILL.md              # 技能文档（每个 skill 必有）
├── scripts/
│   └── {功能名}.py        # 可独立运行
├── references/
│   └── {参考文档}.md
└── data/                  # 运行时数据（自动创建）
    └── {数据文件}.json
```

### Python 脚本头部模板
```python
#!/usr/bin/env python3
"""
{简要描述}

数据源：{列出使用的端点}
Usage:
  python3 {script}.py
  python3 {script}.py --json
"""

import json, sys, os, urllib.request
from datetime import datetime
from typing import Dict, Any, List, Optional

# ========== 数据源 ==========
# {端点 + 编码 + 字段说明}
```

### 输出格式
- `--json` 标志 → 纯 JSON 到 stdout（供下游消费）
- 默认 → 人类可读 Markdown（供 Discord 展示）
- 无信号场景 → 静默退出（return code 0，无 stdout）

## 模块整合数据流（2026-06-09 接通）

缠论与 Serenity 不再是孤岛，已程序化接入主决策链路：

1. **深度面回流**：`four_dim_scorer` 的深度面(20%)优先读 Serenity 深研缓存
   (`common/deep_research_cache.py`)，而非 PE 分桶；深研一次、日评复用，过期向 PE 快照线性衰减。
   Serenity 流程产出 scorecard 后用 `deep_research_cache.py write` 落缓存。
2. **缠论信号化**：`chanlun-backtest/scripts/chan_structure.py` 输出分型/笔/中枢/三买三卖/背驰
   JSON 信号，接入四维技术面与 60 分钟择时。
3. **信号过闸才计权（铁律）**：任何缠论信号在 `research_gate --register` 写入
   `allowed_in_live_agent=true` 之前，只能 display-only / 0 权重（标"研究假设"）。
   裁决统一走 `common/strategy_registry.py`，默认未注册=不计权。
4. **单一事实源**：打板阈值集中在 `config/daban_thresholds.yaml`，实盘候选闸门
   (`daban_candidate_api`)与回测引擎(`daban_bt_engine`)共读；阈值变更只允许在
   `research_gate` 通过后进行，**禁止用实盘结果回拟合**。
5. **闭环门控**：`performance_tracker --gate` 按 `by_strategy` 期望值淘汰负期望策略
   (写 `strategy_registry`)，`recommendation_audit` 对被停用策略仓位归零。
   **淘汰走门控、改规则走闸门——两条路分开，防过拟合。**
6. **大盘 context 回流**（2026-06-10）：`global-market-monitor --cache` 把 `assess_impact`
   落入 `common/market_context.py` 缓存；four_dim 出分后叠加 overlay——大盘系统性承压
   (risk_off) 时个股 grade 降一档并标注，`insufficient_data` 时拒绝写缓存（fail-closed）。
   无缓存 = no-op。
7. **情绪面回流**（2026-06-10，核心玩法主战场）：`hot-money analyze --cache`（连板梯队/
   板块涨停数/封板质量）与 `capital_flow_monitor --cache`（北向/板块/个股主力资金）合并
   写入 `common/signal_context.py`；four_dim 情绪面按打板原生口径加成（连板在册/早盘强封/
   板块赚钱效应集群/主力流向）。缓存缺失时行为与历史完全一致。
8. **催化面分级×时效**（2026-06-10）：`score_catalyst` 按 T1 政策战略(±1.2)/T2 订单业绩
   (±0.8)/T3 泛利好(±0.4) 分级，乘新闻新鲜度衰减——半衰期按催化级别区分
   (2026-06-11)：T1 中央级慢衰减（10天全额/30天0.6，主线寿命15-30交易日），
   T2/T3 快衰减（3天全额/7天0.7，脉冲式3-5日）。
9. **情绪温度计**（2026-06-11，游资方法论核心）：`common/market_temperature.py` 按
   高度板×连板晋级率五档定位（冰点/修复/发酵/加速/极热）——超短先选情绪位置再选股。
   晋级率靠 signal_context 梯队按日滚动（`prev_lianban_ladder`）。约束链路：
   candidate_discovery 输出温度裁决（advice/top_n_limit）、recommendation_audit 对
   daban 策略仓位乘温度倍率（冰点0.3/发酵1.0/加速0.8只做最强/极热0），退潮硬信号
   （昨日高度板今晨跌>5%）强制只出不进。趋势策略不受打板温度约束。
10. **打板排名游资因子**（2026-06-11）：`candidate_pipeline.hot_money_bonus`（0-20附加分）
    注入连板梯队在册/率先封板≤09:45/封单比(≥1%最低≥3%理想)/板块涨停集群——龙头识别
    三条件来自游资选股深度研究报告。signal_ctx 缺失时退化为纯量价排名（Codex 原行为）。
11. **T+1 竞价证伪场景**（2026-06-11）：`daban_candidate_api.t1_scenario` 按封板质量
    分场景 A(强封:竞价≥+3%持有/<0%减半)/B(烂板回封:-4%看承接限1/3补/平开冲高全清)/
    C(未封回:无条件开盘3分钟斩仓)——T+1 制度下竞价出局优于盘中挨核按钮。

## 关键文件索引

| 文件 | 位置 | 用途 |
|------|------|------|
| portfolio.json | `stock-triage/data/` | 持仓数据 |
| signal_history.json | `stock-triage/data/` | 历史信号记录 |
| strategy_registry.json | `stock-triage/data/` | 策略闸门+门控状态（缠论信号过闸/负期望淘汰） |
| deep_research/{code}.json | `stock-triage/cache/` | Serenity 深研缓存（回流四维深度面） |
| market_context.json | `stock-triage/cache/` | 大盘影响缓存（global-monitor --cache 写，四维 overlay 读） |
| signal_context.json | `stock-triage/cache/` | 情绪上下文（hot-money/capital_flow --cache 写，情绪面读） |
| candidate_pool_latest.json | `stock-triage/data/` | 全市场双策略动态观察池 |
| candidate_lifecycle/YYYY-MM-DD.json | `stock-triage/data/` | 候选阶段、淘汰原因与T+1/T+3结果 |
| auction_shortlist_latest.json | `daban-stock-picker/data/` | 09:25竞价前20短名单 |
| open_confirmation_latest.json | `daban-stock-picker/data/` | 09:35最终确认结果 |
| daban_thresholds.yaml | `config/` | 打板阈值单一事实源（实盘=回测，过闸才改） |
| intraday_alerts.json | `stock-triage/data/` | 盘中告警去重缓存 |
| alerts.json | `$HERMES_HOME/cron/output/` | 价格提醒数据 |
| job_runs.json | `$HERMES_HOME/cron/output/` | Cron 运行账本 |
| `{job_id}/{run_id}.json` | `$HERMES_HOME/cron/output/` | 每次 cron 的隔离 artifact |
| .env | `~/.hermes/` | API keys + NO_PROXY |

## 用户偏好（来自记忆）

- 中文交流，分析报告用中文
- 深度参与 A 股，关注板块：封测 / AI 算力 / 军工航天 / 电网 / 家电 / 煤炭
- 跟踪标的：华能国际(600011) / 通富微电(002156) / 长电科技(600584) / 华天科技(002185) / 深科技(000021) / 太极实业(600667)
- 要求明确操作建议，不喜欢模糊分析
- 对分析质量要求极高，'全量扫描'必须是真的全量
- 排障时先沿现有认证链路，不要擅自切换 provider
