---
name: policy-intent-decoder
description: >-
  A-share policy intent decoder. Use this skill whenever the user asks how a
  Chinese policy, official meeting, ministry notice, People's Daily/Xinhua
  article, CSRC/PBOC statement, "稳股市", "活跃资本市场", sector support,
  industry regulation, or macro policy signal affects A-share sectors or stock
  selection. It converts current official sources into an auditable
  `policy_intent_signal_v1` with source hierarchy, real-intent inference,
  policy clock, tool hardness, agency coordination, transmission chain,
  beneficiary/loser maps, and stock-selection support. Do not treat market
  rumors, broker notes, or social-media interpretation as policy intent.
metadata:
  hermes:
    tags: [A股, 政策解读, 官方信号, 选股, 催化, 风险]
    category: finance
---

# A股政策意图解码器

`policy-intent-decoder` 是 A 股系统的政策证据层。它不直接给买卖指令，也不替代
`stock-triage` 的公告、可成交性、组合风险和 T+1 Policy。它的任务是把官方政策信号转成
可审计的选股辅助维度：政策要解决什么问题、用什么工具解决、谁执行、谁受益、谁承压、
市场是否已经定价，以及后续要跟踪什么反证。

## 运行边界

- 只做研究和证据转换，不自动下单，不输出无条件买入。
- 官方原文优先；未给原文时先检索当前官方来源，再读二级解读。
- 传闻、券商快评、自媒体和社媒只可作为市场反应或分歧证据，不可作为政策意图源。
- 不在 Skill、prompt 或 cron 中硬编码股票、板块、持仓或主题。
- 如果政策文本、发布日期、发布机构或执行工具无法确认，结论降级为 `watch_only`。
- 选股支持必须说明传导链和失效条件；不能把“国家支持”直接等同于“公司利润上升”。

## 输入

可以接受以下任一输入：

- 官方政策原文、会议通稿、部门通知、领导讲话、官媒文章或 URL。
- 一条政策类新闻、行业监管新闻、资本市场表述变化。
- 用户给出的行业、板块或候选股票池，用来做政策映射。
- 运行批次内已有的候选证据，由 `stock-triage` 或 `news-to-sector` 传入。

如果没有原文或 URL，先按来源阶梯检索当前材料；如果无法取得官方材料，停止在
`insufficient_source`，不要从记忆或市场解读补全。

## 数据源和时效保障

本技能的数据源目录在 `references/official-policy-sources.json`。默认只把以下一手源当作政策意图源：

- 国务院/中国政府网：政策文件库、政策频道、要闻。
- 新华社：中央会议、国务院常务会议、权威通稿。
- 部委：证监会、人民银行、发改委、财政部、工信部、金融监管总局。
- 交易所：上交所、深交所的交易制度、上市公司和市场运行规则。

`scripts/watch_official_policy.py` 是分钟级采集入口。它做四件事：

1. 按官方源目录轮询官网页面，不从财经媒体或社交平台倒推政策。
2. 提取标题和链接，用来源位阶、政策关键词、工具词和部委协同词打分。
3. 用 `fingerprint` 去重，写入 `$A_STOCK_STATE_HOME/skills/policy-intent-decoder/data/`。
4. 输出 `policy_intent_watch_v1`，只有新信号进入 `signals`，无新信号保持静默。

Cron 任务 `official-policy-watch` 通过 `scripts/run_agent_dag.py` 启动，计划为北京时间
`08:00-22:00` 每 10 分钟运行一次，并设置 `trading_day_policy=calendar_day`，所以周末和节假日也会扫。

这不能承诺“绝不漏报”或“秒级同步”：官网改版、网络失败、反爬、异步发布和未公开内参都可能造成延迟。
可验证的保障是：一手源、分钟级轮询、失败显式记录、去重状态、可回放快照。

## 官方来源阶梯

按位阶判断政策强度。高位阶来源可以压过短期市场噪音。

| 等级 | 来源 | 解读含义 |
| --- | --- | --- |
| S5 | 政治局会议、中央经济工作会议、政府工作报告、国务院文件 | 顶层目标和政策约束，优先级最高 |
| S4 | 证监会、央行、发改委、财政部、工信部、金融监管总局等部委文件或发布会 | 工具和执行路径开始明确 |
| S3 | 新华社通稿、人民日报评论/社论/权威栏目 | 预期管理和合法性铺垫 |
| S2 | 交易所、自律组织、地方政府实施细则 | 执行落点、地方试点或交易规则 |
| S1 | 行业协会、专家访谈、官方媒体二级转述 | 辅助证据，不能单独定性 |
| S0 | 券商研报、财经媒体、自媒体、社媒传闻 | 市场反应或分歧，不是政策源 |

优先读取发布日期、发布机构、是否全文转载、是否有上位文件链接。重大政策要同时检查
国务院/部委/新华社/人民日报之间是否联动。

## 解码流程

### 1. 建立事实底座

提取并记录：

- `published_at`、`issuer`、`source_rank`、原文 URL 或文件路径。
- 主题关键词和政策对象：行业、市场、交易制度、资金端、公司治理、风险处置等。
- 上位政策依据：中央经济工作会议、政府工作报告、国务院文件、部委专项文件。
- 是否是新增表述、延续表述、措辞升级或执行细则。

### 2. 反推真实意图

不要停在“利好/利空”。用这个公式反推：

```text
真实意图 = 要解决的问题 + 可接受的代价 + 配套工具 + 执行主体 + 考核/问责机制
```

常见问题框架：

- 稳预期/防风险：股市、楼市、债务、金融机构、外部冲击。
- 高质量发展/新质生产力：科技创新、产业升级、并购重组、上市公司质量。
- 国家安全/自立自强：核心技术、供应链安全、数据、能源、粮食。
- 共同富裕/投资者保护：分红回购、中小投资者权益、费用让利、财富效应。
- 反内卷/统一大市场：规范竞争、落后产能出清、价格秩序。
- 强监管/法治化：财务造假、违规减持、操纵市场、恶意做空、退市。

同一句话在不同框架下含义不同。框架从“支持发展”漂移到“防风险/强监管”时，风险评级上调。

### 3. 量化信号硬度

给出启发式评分，缺项要标明，不用训练知识补分。

| 维度 | 低强度 | 高强度 |
| --- | --- | --- |
| 来源位阶 | S1-S2 | S4-S5 |
| 措辞强度 | 研究、探索、鼓励 | 专项整治、严格监管、依法查处、问责 |
| 工具硬度 | 原则表述 | 财政资金、货币工具、税收、准入、处罚、司法 |
| 协同等级 | 单一部门 | 多部委、国务院、政治局、公安司法 |
| 市场相关性 | 间接宏观背景 | 直接影响融资、估值、资金供给、交易规则或盈利 |
| 时间紧迫度 | 长期方向 | 已有细则、窗口压缩、专项行动 |

措辞强度参考：

```text
1 研究探索 -> 2 积极推进 -> 3 规范引导 -> 4 有序整治
-> 5 坚决整治 -> 6 依法严惩 -> 7 专项打击
```

有效信号的最低门槛：

- `source_rank >= S4`；或
- `source_rank >= S3` 且 `coordination_level >= L2`；或
- `tool_hardness >= 4` 且能确认执行主体；或
- 出现领导批示、国务院部署、多部委联合、司法入轨等强执行信号。

未过门槛的政策只能作为观察项或市场情绪项。

### 4. 套政策时钟

相同表述在不同时间的含义不同：

| 时间 | 政策时钟 | 解读要点 |
| --- | --- | --- |
| 1-2 月 | 人事/春节窗口 | 大政策少，孤立信号可靠性下降 |
| 3 月 | 两会定调 | 政府工作报告关键词是全年锚点 |
| 4-6 月 | 部署细则期 | 部委文件密集，是执行前奏 |
| 7-8 月 | 公开信号偏少 | 沉默不等于低风险 |
| 9-10 月 | 专项执行期 | 整治、检查、落地速度加快 |
| 11-12 月 | 次年定调 | 中央经济工作会议前瞻价值高 |

如果政策已经有细则、额度、试点名单或执法案例，时间窗口优先按执行材料判断。

### 5. 建立 A 股传导链

必须把政策映射到可验证的市场变量：

```text
顶层定调 -> 工具落地 -> 执行主体 -> 企业经营变量
-> 资金/估值变量 -> 板块/个股候选 -> 市场反馈 -> 政策再调节
```

至少拆出以下影响：

- `earnings_path`：收入、价格、订单、成本、产能利用率、行业集中度。
- `discount_rate_path`：利率、流动性、风险偏好、长期资金供给。
- `risk_premium_path`：监管、退市、财务造假、政策不确定性。
- `capital_supply_path`：IPO/再融资、并购重组、回购分红、中长期资金入市。
- `competition_path`：反内卷、准入、国产替代、落后产能退出。

### 6. 转成选股支持

只给“选股维度支持”，不越过 Triage 输出交易动作。

输出三类映射：

- `direct_beneficiaries`：政策工具直接改善订单、融资、估值或监管环境的方向。
- `indirect_beneficiaries`：上游、设备、渠道、服务商、替代品或补短板环节。
- `pressure_targets`：被限产、降价、强监管、去杠杆、退市、反内卷约束的方向。

对每个方向写清楚：

- 受益机制，不只写板块名。
- 需要验证的数据：价格、订单、招标、资金流、公告、分红回购、减持、执法案例。
- 可能已被定价的迹象：放量、连续涨停、估值扩张、拥挤度、新闻密度。
- 失效条件：配套工具缺失、部门口径不一致、市场过热被监管降温、企业利润无法兑现。

## 输出协议

默认先给 5-8 行中文结论，再输出结构化块。结构化块用于 `stock-triage` 或人工复核。

```json
{
  "schema_version": "policy_intent_signal_v1",
  "asof": "YYYY-MM-DDTHH:MM:SS+08:00",
  "status": "ready | watch_only | insufficient_source",
  "research_only": true,
  "trading_action": "none",
  "policy_item": {
    "title": "",
    "published_at": "",
    "issuer": "",
    "source_rank": "S0-S5",
    "source_url": "",
    "is_primary_source": true
  },
  "real_intent": {
    "problem_to_solve": "",
    "narrative_frame": "",
    "acceptable_costs": [],
    "policy_stage": "direction | deployment | execution | enforcement | normalization"
  },
  "signal_hardness": {
    "wording_level": 1,
    "tool_hardness": 0,
    "coordination_level": "L0-L5",
    "market_relevance": 0,
    "policy_clock": "",
    "confidence": "low | medium | high"
  },
  "transmission_chain": [
    {
      "step": "",
      "mechanism": "",
      "verification": ""
    }
  ],
  "selection_support": {
    "direct_beneficiaries": [],
    "indirect_beneficiaries": [],
    "pressure_targets": [],
    "valuation_or_funding_effect": "",
    "should_feed_four_dim_catalyst": false
  },
  "risk_controls": {
    "priced_in_risk": "",
    "policy_reversal_or_delay_risk": "",
    "data_gaps": [],
    "blocked_reason": ""
  },
  "watchpoints": [
    {
      "signal": "",
      "why_it_matters": "",
      "source_to_check": ""
    }
  ]
}
```

## 与其他技能协作

- `news-to-sector`：遇到政策类新闻时，先用本技能判断真实意图和政策硬度，再做板块传导。
- `stock-analyst`：把政策信号作为催化或风险证据，不替代技术、财务和公告检查。
- `stock-triage`：只有 `status=ready`、来源可核验且传导链明确时，才允许进入方向性建议链路。
- `serenity-investment-research`：对长期产业政策、国家安全和新质生产力主题做深度公司研究。
- `social-sentiment`：只用于验证市场是否关注或过热，不能倒推出官方意图。

## 快速判读

- 顶层定调 + 硬工具 + 多部门协同 = 可进入选股候选证据。
- 官媒铺垫 + 无工具 = 观察，不要急着选股。
- 支持性表述转为规范/整治 = 框架漂移，先看风险再看机会。
- 稳市场表述通常是降低系统性风险和稳定预期，不等于承诺单边上涨。
- 强监管可能短期压估值，但会提高高治理、分红、现金流稳定公司的相对质量溢价。
