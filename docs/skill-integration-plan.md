# 三个 OpenClaw 技能接入 A-stock Agent System 方案

生成日期：2026-07-07

## 结论

这三个技能不应原样作为聊天提示接入，而应拆成可调度、可测试、可复用的确定性模块：

1. `捕捉公司事件机会`：补齐“公司特殊事件 -> 机会/风险定价”的结构化事件层，接在公告、新闻、事件日历和候选池之间。
2. `晨会纪要`：补齐 07:00 的“给人看的盘前总控摘要”，聚合隔夜、政策、公司事件、候选池和今日日历，不替代 08:50/09:27/09:36 的战术简报。
3. `行为金融分析`：把情绪周期、过度反应/反应不足、拥挤与去偏清单统一成 `behavioral_finance_context`，先作为研究/风险解释层，过研究门后再影响 live ranking。

现有系统的核心约束保持不变：所有 cron 入口必须走 `python scripts/run_agent_dag.py <job-id> --emit-target`；运行态写入 `$A_STOCK_STATE_HOME`；方向性建议必须经过公告、数据质量、可交易性、价格计划、组合风险、T+1 规则和研究门。

## 已读材料

OpenClaw 技能：

- `/Users/eleven/.openclaw/workspace/skills/捕捉公司事件机会/SKILL.md`
- `/Users/eleven/.openclaw/workspace/skills/捕捉公司事件机会/references/event-framework.md`
- `/Users/eleven/.openclaw/workspace/skills/捕捉公司事件机会/references/output-template.md`
- `/Users/eleven/.openclaw/workspace/skills/晨会纪要/SKILL.md`
- `/Users/eleven/.openclaw/workspace/skills/行为金融分析/behavioral-finance/SKILL.md`

A-stock 现有架构重点：

- 调度源：`cron/hermes-cron-manifest.json`
- DAG 入口：`scripts/run_agent_dag.py`
- 隔离执行与 artifact：`scripts/hermes_job_runner.py`、`skills/common/runtime_context.py`
- 状态路径：`skills/common/paths.py`
- 新闻/催化缓存：`skills/news-to-sector/scripts/scheduled_monitor.py`、`scripts/realtime_catalyst_trigger.py`、`skills/common/catalyst_context.py`
- 早盘战术简报：`scripts/market_intelligence_brief.py`
- 行为/拥挤已有基础：`skills/common/behavior_risk.py`、`skills/common/crowding_fragility.py`、`skills/common/emotion_cycle_features.py`
- 事件/公告已有基础：`skills/stock-triage/scripts/event_calendar.py`、`skills/common/announcement_risk.py`、`skills/common/stock_intelligence.py`

## 现有系统形状

当前 DAG 已经有完整的交易日节奏：

- 08:15 `global-preopen`
- 08:40 `hot-money-context-backfill`
- 08:42 `social-attention-preopen`
- 08:45 `candidate-preopen`
- 08:50 `preopen-intelligence-brief`
- 09:15-09:27 集合竞价链路
- 09:36 `open-intelligence-brief`
- 盘中新闻、催化、资金流、持仓监控
- 15:02-15:55 收盘候选、四维评分、组合检查、研究分发、FSM 扫描
- 16:10 / 周末绩效与研究门

重要设计点：

- 业务脚本只做数据和结构化输出；`agent_job_runner.py` 统一写 artifact、run ledger、snapshot。
- `context_from` 只应表达同一批次的硬依赖；跨日或最新可用数据，最好由业务脚本显式读取最新 artifact/cache，避免盘前被缺失依赖阻塞。
- `local` 交付用于回流上下文，`origin`/`feishu_direct` 才会对人输出。
- 已有 `catalyst_context.json` 是公司级/新闻级事件回流四维评分与盘中监控的自然入口。

## 技能一：捕捉公司事件机会

### 核心能力

该技能是“特殊情况分析师”框架，覆盖：

- 并购重组、吸收合并、借壳、重大资产重组
- 资产注入、国企改革、整体上市、混改、员工持股
- 回购、控股股东/高管增持
- 分拆上市
- 指数纳入/剔除
- 解禁减持
- 管理层变更

输入：

- 事件范围：全市场、行业、公司、持仓/候选池/监控列表
- 事件类型：全部或指定类别
- 时间窗口：活跃事件或历史复盘
- 风险偏好：保守/积极
- 结果数量：默认 5 个

输出：

- 活跃机会汇总
- 单公司事件详情
- 价差/上行、失败下行、成功概率、期望值
- 里程碑、监管/资金/股东/时间/信息不对称风险
- 历史可比案例
- 风险矩阵与“参与/关注/回避”

### 接入后解决的痛点

现有系统已经能抓新闻、政策、解禁、分红、公告风险，但缺少一层“公司事件的投资语义”：

- `event_calendar.py` 偏日历提醒，不能估算事件价差、成功概率、时间风险。
- `scheduled_monitor.py` 能把新闻放进 `catalyst_context`，但事件类型和收益/风险结构较粗。
- `announcement_risk.py` 偏负面拦截，缺少对回购、增持、重组、分拆这类正向/中性特殊情况的结构化分析。
- `candidate_discovery` 和 `four_dim_scorer` 需要更干净的 `event_type`、`milestone`、`expected_value`、`risk_flags`，而不是从新闻标题临时猜。

### 推荐位置

新增一个业务 skill + 一个 common 事件模型：

- `skills/company-event-opportunities/SKILL.md`
- `skills/company-event-opportunities/references/event-framework.md`
- `skills/company-event-opportunities/references/output-template.md`
- `skills/company-event-opportunities/scripts/scan.py`
- `skills/common/company_event_opportunities.py`
- `skills/common/company_event_schema.py`
- `config/company_event_opportunities.yaml`
- `tests/test_company_event_opportunities.py`
- `tests/test_company_event_opportunity_scan.py`

不建议只放进 `skills/common/`。原因是它既有可调度业务脚本，又有一套可给人工阅读/复盘的技能说明。`common/` 只承载 schema、分类器、评分函数和 cache 读写。

### 改造要点

数据源：

- 公司公告：复用 `skills/common/announcement_risk.py` 的 CNINFO 检索能力，并扩展关键词组。
- 已有动态标的：复用 `skills/common/runtime_targets.py`，覆盖持仓、监控、候选池。
- 新闻事件：读取 `catalyst_context.json`、`news_pipeline` L2 结果和 `scheduled_monitor` artifact。
- 解禁/分红/机构/股东：复用 `stock_intelligence.py` 与 `event_calendar.py`。
- 指数调整：先作为配置/新闻识别项接入；如后续有稳定指数公告源，再做独立 provider。

结构化输出建议：

```json
{
  "schema": "company_event_opportunities_v1",
  "generated_at": "...",
  "trading_date": "2026-07-07",
  "scope": {"universe": "runtime_targets", "event_types": ["all"]},
  "status": "ready",
  "opportunities": [
    {
      "code": "600000",
      "name": "示例股份",
      "event_type": "buyback",
      "event_status": "announced",
      "source_rank": "S4",
      "evidence": [{"title": "...", "url": "...", "published_at": "..."}],
      "announced_at": "...",
      "milestones": [{"date": "...", "label": "...", "status": "pending"}],
      "upside_pct": 8.0,
      "downside_pct": -5.0,
      "success_probability": 0.65,
      "expected_value_pct": 3.55,
      "time_horizon_days": 60,
      "annualized_return_if_success_pct": 48.0,
      "risk_level": "medium",
      "risk_flags": ["approval_risk", "time_risk"],
      "suggestion": "watch",
      "directional_ready": false
    }
  ],
  "summary": {"opportunity_count": 1, "risk_event_count": 0}
}
```

写入位置：

- `$A_STOCK_STATE_HOME/skills/company-event-opportunities/data/latest.json`
- `$A_STOCK_STATE_HOME/skills/company-event-opportunities/data/history/{trading_date}.json`
- `$A_STOCK_STATE_HOME/skills/stock-triage/cache/catalyst_context.json`
- 可选：高重要事件进入 `$A_STOCK_STATE_HOME/skills/stock-triage/data/monitor_registry.json`

关键纪律：

- `success_probability` 和 `upside_pct` 缺少足够公开证据时必须为 `null` 或低置信，不得用默认乐观值。
- 事件失败下行必须显式给出或标记 unavailable；不能只有利好叙事。
- 任何正向建议进入 `signal_ledger` 前，仍需走现有 `decision_policy` 和 T+1 检查。
- 首期只做 `watch/review/avoid`，不直接生成买入建议。

### DAG 集成

新增 cron job：

```json
{
  "id": "company-event-opportunity-scan",
  "name": "公司事件机会扫描",
  "schedule": "35 8 * * 1-5",
  "timezone": "Asia/Shanghai",
  "command": "python scripts/run_agent_dag.py company-event-opportunity-scan --emit-target",
  "cwd": ".",
  "enabled": true,
  "external": true,
  "expected_output": "json",
  "silent_when_no_signal": true,
  "execution_mode": "isolated_subprocess",
  "context_scope": "cron",
  "deliver": "local",
  "max_output_chars": 3000,
  "context_from": [],
  "artifact_path_template": "{cron_output_dir}/{job_id}/{run_id}.json",
  "allowed_state_writes": [
    "$A_STOCK_STATE_HOME/skills/company-event-opportunities/data/",
    "$A_STOCK_STATE_HOME/skills/stock-triage/cache/catalyst_context.json",
    "$A_STOCK_STATE_HOME/cron/output/company-event-opportunity-scan/",
    "$A_STOCK_STATE_HOME/cron/output/job_runs.json"
  ],
  "run": {
    "command": "python skills/company-event-opportunities/scripts/scan.py --scope runtime --json",
    "cwd": ".",
    "timeout_seconds": 180
  }
}
```

候选池接入方式：

- 第一阶段：`candidate-preopen` 不加硬依赖，只让 `scan.py` 写入 `catalyst_context`，由四维评分和盘中监控读取。
- 第二阶段：在 `candidate_discovery.py` 中读取 `company_event_opportunities/latest.json`，作为 `recall_source=company_event` 的候选召回来源，但仍通过候选 FSM 和所有门禁。
- 第三阶段：高置信事件进入 `research_bus`，触发 `event_review` 或 `anomaly_review`，由 `research-committee` 深挖。

推荐调度：

- 08:35 交易日：盘前活跃事件扫描，赶在 `candidate-preopen` 和 08:50 简报前完成。
- 12:20 交易日可选：午间公告/新闻补扫，交付 `local`，只回流上下文。
- 20:30 交易日可选：晚间公告扫描，若有重大重组/回购/减持再飞书直推。
- 周六 09:30：全量特殊情况周报，适合慢事件如国企改革、资产注入、分拆上市。

## 技能二：晨会纪要

### 核心能力

该技能是 2 分钟可读的 morning note 模板：

- 隔夜/盘前动态
- 盈利、指引、公司新闻、并购、管理层、监管、宏观
- 今日关键事件
- Top Call：最重要的一句话
- Trade Ideas：有则给出，没有就明确“无重大变化”
- 快速业绩反应表

输入：

- 覆盖范围：持仓、监控、候选池、主题
- 最新 global/news/policy/company event artifacts
- 今日交易日和事件日历
- 可选分析师名/覆盖行业

输出：

- 一页以内 Markdown
- 可选 JSON：供 artifact、复盘和后续摘要压缩

### 接入后解决的痛点

现有 `market_intelligence_brief.py` 是战术阶段简报：

- 08:50 读候选池；
- 09:27 读竞价；
- 09:36 读开盘确认。

它不负责 07:00 前的“隔夜发生了什么、今天盯什么、最重要的一句话是什么”。所以用户会在开盘前缺一个总控视图。

### 推荐位置

新增渲染脚本，不替代现有简报：

- `scripts/morning_note.py`
- `skills/common/morning_note.py`
- `templates/morning_note.md` 或 `skills/a-stock-daily-report/templates/morning_note.md`
- `tests/test_morning_note.py`
- `tests/test_morning_note_cron.py`

如果未来要放进技能层，可新增：

- `skills/a-stock-morning-note/SKILL.md`

但首期更建议脚本化，因为它是系统汇总产物，不是单一分析技能。

### 改造要点

读取顺序：

1. 最新 `global-evening` / `global-preopen` artifact。
2. 最新 `news-monitor`、`news-monitor-intraday`、`official-policy-watch` artifact。
3. 最新 `company-event-opportunity-scan` 输出。
4. `event_calendar.py --json` 的今日窗口。
5. 当前 `portfolio.json`、`monitor_registry.json`、`candidate_pool_latest.json`。
6. 如有 `behavioral_finance_context.json`，加入市场心理/仓位去偏提醒。

输出字段建议：

```json
{
  "schema": "morning_note_v1",
  "generated_at": "...",
  "trading_date": "2026-07-07",
  "status": "ready",
  "top_call": "...",
  "overnight_developments": [],
  "company_events": [],
  "policy_macro": [],
  "key_events_today": [],
  "trade_ideas": [],
  "risk_watch": [],
  "missing_inputs": []
}
```

Markdown 输出建议：

```markdown
## 2026-07-07 Morning Note

**Top Call**：...

**隔夜/盘前**
- ...

**公司事件**
- ...

**今日重点**
- ...

**交易想法**
- ...

**风险**
- ...
```

关键纪律：

- 没有重大信息时明确输出“隔夜无重大变化，维持原计划”，不要硬凑。
- `trade_ideas` 只引用已经过现有门禁或标记为研究观察的对象。
- 输出控制在 `max_output_chars` 内，适合飞书/Discord 直读。
- 缺上游时列 `missing_inputs`，但不阻塞生成，除非交易日历不可用。

### DAG 集成

新增 cron job：

```json
{
  "id": "morning-note",
  "name": "07点晨会纪要",
  "schedule": "0 7 * * 1-5",
  "timezone": "Asia/Shanghai",
  "command": "python scripts/run_agent_dag.py morning-note --emit-target",
  "cwd": ".",
  "enabled": true,
  "external": true,
  "expected_output": "text",
  "silent_when_no_signal": false,
  "execution_mode": "isolated_subprocess",
  "context_scope": "cron",
  "deliver": "feishu_direct",
  "max_output_chars": 4500,
  "context_from": [],
  "artifact_path_template": "{cron_output_dir}/{job_id}/{run_id}.json",
  "allowed_state_writes": [
    "$A_STOCK_STATE_HOME/cron/output/morning-note/",
    "$A_STOCK_STATE_HOME/cron/output/job_runs.json"
  ],
  "run": {
    "command": "python scripts/morning_note.py --json-lines-safe",
    "cwd": ".",
    "timeout_seconds": 30
  }
}
```

为什么 `context_from` 为空：

- 07:00 时同批次通常没有当天上游 artifact。
- 该脚本应自己读取“最新可用”的昨晚/隔夜 artifact，并把缺失项写进 `missing_inputs`。
- 这避免晨会纪要因为 08:15 `global-preopen` 尚未运行而被 DAG 阻塞。

推荐调度：

- 07:00 交易日：主晨会纪要，直接推送。
- 08:50 保留现有 `preopen-intelligence-brief`：候选池战术简报。
- 09:27/09:36 保留集合竞价和开盘简报。
- 15:35 `closing-triage` 之后可选 `evening-note`，作为次日晨会素材，不一定推送。

## 技能三：行为金融分析

### 核心能力

该技能把行为金融理论翻成交易信号和风控规则：

- 反应不足 -> 动量延续
- 过度反应 -> 短期/长期反转
- 认知偏差清单：损失厌恶、过度自信、锚定、确认偏误、近因偏误等
- 群体行为：羊群、信息瀑布、注意力效应
- 情绪周期：恐惧、谨慎、乐观、兴奋、亢奋、否认、恐慌
- 综合情绪分数：成交、融资、新高、涨停、封基折价等
- 动量策略优化：区分基本面动量和情绪动量，缩短高关注票持有期
- 极端恐惧/贪婪反向信号

输入：

- 全市场行情快照、成交额、涨跌停数量、新高比例
- 融资融券、封基折价、新开户等可得数据
- 个股 K 线、成交量、VWAP/筹码近似
- 社会关注度、板块相关性、候选池/持仓
- signal ledger 的历史交易行为

输出：

- 市场情绪诊断
- 过度反应/反应不足信号
- 羊群/拥挤/注意力风险
- 动量持有期调整建议
- 仓位暴露建议
- 去偏 checklist

### 接入后解决的痛点

系统已有三个分散基础：

- `behavior_risk.py`：评估 Agent 自身是否因近期胜负发生行为漂移。
- `crowding_fragility.py`：评估市场/板块拥挤和脆弱性。
- `emotion_cycle_features.py`：把情绪周期口径变成个股技术特征，但目前未过研究门前 0 权重。

缺口是缺少统一的市场行为金融上下文：

- 同一套情绪/过度反应/去偏结果没有集中输出。
- `four_dim_scorer`、`decision_policy`、`morning_note` 各自只能读到碎片。
- 高关注高换手票的持有期缩短、极端情绪降/升暴露等规则没有统一配置。

### 推荐位置

新增一个 common 聚合层和一个 digest 脚本：

- `skills/common/behavioral_finance.py`
- `scripts/behavioral_finance_digest.py`
- `config/behavioral_finance.yaml`
- `tests/test_behavioral_finance.py`
- `tests/test_behavioral_finance_digest.py`

保留现有模块：

- `behavior_risk.py` 继续评估 Agent 行为漂移。
- `crowding_fragility.py` 继续作为市场/板块拥挤指标来源。
- `emotion_cycle_features.py` 继续作为个股情绪周期特征来源。

不要引入 `pandas/numpy/scipy` 作为硬依赖。OpenClaw 技能写了这些依赖，但当前 repo 已经大量使用纯标准库和现有指标函数；首期应沿用现有模式，避免 cron 环境新增不稳定依赖。

### 改造要点

建议 `behavioral_finance.py` 暴露：

```python
build_behavioral_finance_context(
    market_snapshot: dict,
    social_attention: dict | None,
    hot_money_context: dict | None,
    signal_state: dict | None,
    *,
    asof: str,
) -> dict
```

输出 schema：

```json
{
  "schema": "behavioral_finance_context_v1",
  "generated_at": "...",
  "trading_date": "2026-07-07",
  "status": "ready",
  "sentiment_score": 72,
  "sentiment_phase": "optimism_to_excitement",
  "overreaction": {
    "market": [],
    "stocks": []
  },
  "underreaction": {
    "stocks": []
  },
  "crowding_fragility": {},
  "agent_behavior_risk": {},
  "strategy_adjustments": {
    "momentum_holding_days_multiplier": 0.5,
    "exposure_band": "normal",
    "notes": []
  },
  "debiasing_checklist": [],
  "unavailable": []
}
```

指标来源映射：

- `turnover_ratio`：全市场快照或可得成交额/流通市值近似，缺失则 unavailable。
- `limit_up_count` / `limit_down_count`：热钱/涨停池上下文。
- `new_high_ratio`：全市场日线快照可得时计算，否则 unavailable。
- `margin_growth`：复用 `eastmoney_intelligence.fetch_margin_trading` 的缓存结果。
- `fund_discount` / `new_account_openings`：首期 unavailable，不造数。
- `sector_correlation`：可先用板块成员涨跌同向比例近似；若缺少成分股快照则 unavailable。
- `CGO`：用 60 日 VWAP 近似 `capital_gain_overhang`，复用 K 线与成交量。

接入四维评分：

- 在 `four_dim_scorer.py` 的 `sentiment` 或 `technical` 解释中增加 `behavioral_finance` 子块。
- 初期只给 `notes` 和风险扣分提示，不改变最终分。
- 只有当 `strategy_registry` 中对应策略通过 OOS 研究门，才允许影响 live ranking。

接入决策策略：

- `decision_policy.py` 可读取 `behavioral_finance_context`。
- 极端贪婪只允许降暴露或收紧条件；极端恐惧只允许提高“关注/研究”优先级，不允许绕过买入门禁。

接入晨会：

- `morning_note.py` 读取 `behavioral_finance_context`，输出一句市场心理与去偏提醒，例如“注意连胜后放大仓位”或“高关注票缩短验证窗口”。

### DAG 集成

新增两个 job，先 `local` 回流，再视质量决定是否推送：

```json
{
  "id": "behavioral-finance-preopen",
  "name": "行为金融盘前上下文",
  "schedule": "43 8 * * 1-5",
  "timezone": "Asia/Shanghai",
  "command": "python scripts/run_agent_dag.py behavioral-finance-preopen --emit-target",
  "cwd": ".",
  "enabled": true,
  "external": true,
  "expected_output": "json",
  "silent_when_no_signal": true,
  "execution_mode": "isolated_subprocess",
  "context_scope": "cron",
  "deliver": "local",
  "max_output_chars": 3000,
  "context_from": [
    "social-attention-preopen"
  ],
  "dependency_policy": {
    "trading_date": "same_trading_date",
    "max_age_minutes": 90,
    "optional_jobs": [
      "social-attention-preopen"
    ]
  },
  "artifact_path_template": "{cron_output_dir}/{job_id}/{run_id}.json",
  "allowed_state_writes": [
    "$A_STOCK_STATE_HOME/skills/stock-triage/cache/behavioral_finance_context.json",
    "$A_STOCK_STATE_HOME/cron/output/behavioral-finance-preopen/",
    "$A_STOCK_STATE_HOME/cron/output/job_runs.json"
  ],
  "run": {
    "command": "python scripts/behavioral_finance_digest.py --stage preopen --json",
    "cwd": ".",
    "timeout_seconds": 60
  }
}
```

```json
{
  "id": "behavioral-finance-close",
  "name": "行为金融收盘诊断",
  "schedule": "12 15 * * 1-5",
  "timezone": "Asia/Shanghai",
  "command": "python scripts/run_agent_dag.py behavioral-finance-close --emit-target",
  "cwd": ".",
  "enabled": true,
  "external": true,
  "expected_output": "json",
  "silent_when_no_signal": true,
  "execution_mode": "isolated_subprocess",
  "context_scope": "cron",
  "deliver": "local",
  "max_output_chars": 3000,
  "context_from": [
    "hot-money-context",
    "social-attention-close"
  ],
  "dependency_policy": {
    "trading_date": "same_trading_date",
    "max_age_minutes": 90,
    "optional_jobs": [
      "social-attention-close"
    ]
  },
  "artifact_path_template": "{cron_output_dir}/{job_id}/{run_id}.json",
  "allowed_state_writes": [
    "$A_STOCK_STATE_HOME/skills/stock-triage/cache/behavioral_finance_context.json",
    "$A_STOCK_STATE_HOME/cron/output/behavioral-finance-close/",
    "$A_STOCK_STATE_HOME/cron/output/job_runs.json"
  ],
  "run": {
    "command": "python scripts/behavioral_finance_digest.py --stage close --json",
    "cwd": ".",
    "timeout_seconds": 90
  }
}
```

推荐调度：

- 08:43：盘前轻量版，读昨日收盘和最新关注度，为候选池/晨间解释提供上下文。
- 15:12：收盘完整版，在 `hot-money-context`、`social-attention-close` 后、`four-dim-scorer` 前生成。
- 周日 10:20 可选：行为金融周报，重点复盘极端情绪、过度交易和策略漂移。

## 文件路径规划汇总

建议新增：

- `skills/company-event-opportunities/SKILL.md`
- `skills/company-event-opportunities/references/event-framework.md`
- `skills/company-event-opportunities/references/output-template.md`
- `skills/company-event-opportunities/scripts/scan.py`
- `skills/common/company_event_schema.py`
- `skills/common/company_event_opportunities.py`
- `config/company_event_opportunities.yaml`
- `scripts/morning_note.py`
- `skills/common/morning_note.py`
- `templates/morning_note.md`
- `skills/common/behavioral_finance.py`
- `scripts/behavioral_finance_digest.py`
- `config/behavioral_finance.yaml`
- `tests/test_company_event_opportunities.py`
- `tests/test_company_event_opportunity_scan.py`
- `tests/test_morning_note.py`
- `tests/test_behavioral_finance.py`
- `tests/test_behavioral_finance_digest.py`

建议修改：

- `cron/hermes-cron-manifest.json`
- `scripts/validate_cron_manifest.py` 如有固定脚本白名单
- `tests/test_cron_manifest.py`
- `skills/stock-triage/scripts/candidate_discovery.py` 第二阶段再接 `recall_source=company_event`
- `skills/stock-triage/scripts/four_dim_scorer.py` 增加 `behavioral_finance` 解释块
- `skills/common/evidence_pack.py` 增加公司事件与行为金融上下文挂载
- `skills/common/decision_policy.py` 只做保守方向的风险调整
- `scripts/agent_runtime_context.py` 可选暴露 `behavioral_finance` 和最新 `company_events`

运行态写入：

- `$A_STOCK_STATE_HOME/skills/company-event-opportunities/data/latest.json`
- `$A_STOCK_STATE_HOME/skills/company-event-opportunities/data/history/*.json`
- `$A_STOCK_STATE_HOME/skills/stock-triage/cache/behavioral_finance_context.json`
- `$A_STOCK_STATE_HOME/skills/stock-triage/cache/catalyst_context.json`
- `$A_STOCK_STATE_HOME/cron/output/<job-id>/*.json`

## 推荐实施顺序

### Phase 1：只读接入，不影响交易决策

1. 新增 `morning_note.py`，先解决 07:00 人类可读摘要。
2. 新增 `company_event_opportunities.py` 和 `scan.py`，只写事件机会 JSON 和 `catalyst_context`。
3. 新增 `behavioral_finance.py` 和 `behavioral_finance_digest.py`，只写 context JSON。
4. 新增 cron jobs，全部先 `local` 或低噪声推送。
5. 补 `test_cron_manifest.py`、schema tests、无上游数据时的降级 tests。

### Phase 2：进入证据包和研究平面

1. `evidence_pack.py` 挂载 `company_event_opportunities` 与 `behavioral_finance_context`。
2. `research_dispatch.py` 对高影响公司事件创建 `event_review`。
3. `morning_note.py` 汇总行为金融与公司事件。
4. 高风险公司事件进入 `monitor_registry`，但只作为观察，不自动建议交易。

### Phase 3：经研究门后影响排序

1. 对 `company_event_recall:v1`、`behavioral_finance_overlay:v1` 建立 research gate。
2. 通过 OOS 后允许有限影响 `four_dim_scorer` 或候选池排序。
3. 所有正向影响必须可被 `recommendation_audit.py` 追踪到 reason code。

## 调度时间总表

| Job | 时间 | 交付 | 目的 |
| --- | --- | --- | --- |
| `morning-note` | 07:00 交易日 | `feishu_direct` | 人类晨会总控摘要 |
| `company-event-opportunity-scan` | 08:35 交易日 | `local` | 盘前公司事件结构化回流 |
| `behavioral-finance-preopen` | 08:43 交易日 | `local` | 盘前行为金融轻量上下文 |
| `preopen-intelligence-brief` | 08:50 交易日 | 保持现状 | 候选池战术简报 |
| `behavioral-finance-close` | 15:12 交易日 | `local` | 收盘行为金融完整版 |
| `company-event-opportunity-midday` | 12:20 可选 | `local` | 午间公告/事件补扫 |
| `company-event-opportunity-evening` | 20:30 可选 | `feishu_direct` 仅重大事件 | 晚间公告重大事件提醒 |
| `company-event-opportunity-weekly` | 周六 09:30 | `feishu_direct` 或 `local` | 特殊情况周报 |

## 验证清单

实现时至少跑：

```bash
python scripts/validate_cron_manifest.py
python -m pytest tests/test_cron_manifest.py tests/test_company_event_opportunities.py tests/test_morning_note.py tests/test_behavioral_finance.py -q
python -m ruff check scripts skills tests
git diff --check
```

手工 smoke：

```bash
A_STOCK_STATE_HOME=/Users/eleven/.hermes python scripts/run_agent_dag.py morning-note --runtime openclaw --emit-target
A_STOCK_STATE_HOME=/Users/eleven/.hermes python scripts/run_agent_dag.py company-event-opportunity-scan --runtime openclaw
A_STOCK_STATE_HOME=/Users/eleven/.hermes python scripts/run_agent_dag.py behavioral-finance-close --runtime openclaw
```

验收标准：

- 任何数据源失败都在 `errors` / `unavailable` 中显式出现。
- 没有上游数据时 morning note 仍能生成短摘要，不冒充完整。
- 公司事件不会绕过候选 FSM、公告风险、可交易性、组合风险和 T+1。
- 行为金融初期只解释和降风险，不直接提高 live ranking。
- cron artifact 有 `trading_date`、`batch_id`、`status`、`summary`、`has_signal`。
