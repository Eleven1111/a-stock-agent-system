# A股交易与监控生命周期

## 目标

Hermes 与 OpenClaw 只负责运行同一套确定性脚本。交易规则、推荐质检和监控状态
不依赖对话记忆，避免不同 Agent 给出互相冲突的建议。

## 共享状态

同机两端设置相同目录；跨机器时必须是同一个共享挂载卷：

```bash
export A_STOCK_STATE_HOME="$HOME/.a-stock-agent"
```

`A_STOCK_STATE_HOME` 只存放 A 股业务状态；`HERMES_HOME` 仍用于 Hermes 安装、
Python 虚拟环境和 `.env`。两者不要混用。

共享文件：

- `skills/stock-triage/data/portfolio.json`
- `skills/stock-triage/data/recommendations.json`
- `skills/stock-triage/data/monitor_registry.json`
- `skills/stock-triage/data/trade_history.json`
- `skills/stock-triage/data/signal_ledger.jsonl`
- `market/snapshots/{trading_date}/{job_id}/{snapshot_id}.json`
- `agent_state/agent_state_latest.json`

跨模块生命周期以 append-only `signal_ledger.jsonl` 为规范账本。旧 JSON 文件继续作为
兼容视图使用。完整事件和迁移说明见
[`architecture-hardening.md`](architecture-hardening.md)。

`agent_state_latest.json` 还包含由规范账本投影得到的 `behavior_risk`。它统计连胜后
动作扩张、连亏后追损、动作频率漂移和策略集中度；指定 `asof` 时会排除未来信号，
目前作为运行时第二观察者暴露给 Hermes/OpenClaw，不伪装成已验证的选股 alpha。

## Hermes / OpenClaw 执行入口

两端都调用同一个 runtime-neutral 入口，不把业务脚本直接塞进 Agent 对话：

```bash
python scripts/run_agent_dag.py global-preopen --runtime hermes --emit-target
python scripts/run_agent_dag.py global-preopen --runtime openclaw --emit-target
python scripts/agent_runtime_context.py
```

任务输出写 artifact 和不可变市场快照，依赖门禁失败时 fail-closed。Agent 解释和推送
前通过 `agent_runtime_context.py` 刷新并读取 `agent_state_latest.json`，不从定时任务
聊天上下文猜测持仓、监控或信号状态。DAG 运行租约只在两端共享同一物理状态目录时
具备跨运行时互斥能力。

## T+1 执行约束

`portfolio_manager.py` 按 lot 保存每次买入/加仓日期。当日新增股份被锁定：

- 当日全仓卖出会返回 `T1_LOCKED`
- 返回 `earliest_sell_date` 和 `locked_shares`
- 推荐审计同样拒绝当日 `sell/reduce`
- 止损触发但仍锁定时，只能记录风险和次交易日处置计划

交易日来自 `config/a_share_calendar.json`。每年开市前必须按上交所/深交所休市
通知更新该文件；未覆盖年份会退化为工作日判断，并在结果中标记
`calendar_covered=false`。

## 打板交易纪律熔断

`config/daban_thresholds.yaml` 的 `market_gate` 段是打板专属的账户级熔断线：
本周新开仓≥3笔、日亏≥2%、周亏≥5%、连续错单≥3次、单只打板仓位超过
`position_time_stop_trading_days`个交易日未走出行情。`trading_discipline.py`
读取真实 `signal_ledger.jsonl` 的 `trade.executed` 事件和真实持仓市值算出前四
个数字；`decision_policy` 只在 `strategy_lane == "daban"` 时据此把决策强制降级
为 `avoid`，不影响趋势策略的仓位节奏。三处实盘调用点都已接线：09:26
`auction-finalize`、09:35 `open-confirmation`（最终写 `recommendations.json` 的
关口）、以及独立 CLI `recommendation_audit.py --record`。触发的 `reasons` 会
同时出现在 `preopen_decisions`/`signals` 和 `--json` 报告里，不是只写在配置
文件里的静态规则。时间止损和止盈目标线在 `portfolio_manager.py` 的持仓刷新
里生效，仅在打板来源仓位（`lane == "daban"`，开仓时从最近一条
`signal.opened` 事件自动识别）上触发，均为提示性告警，不自动下单。

`discipline_review.py` 是每日收盘后的执行纪律复盘：对比当日 buy/add 建议与
`trade.executed` 实际成交，标出追价、超仓位、未跟单三类偏离；同时汇总当前
尚未处理的持仓纪律信号（止损/回撤止盈/时间止损/止盈目标）和账户熔断状态。
只陈述差异，不判断对错——跟不跟单可能都是当时合理的临场决策。

## 09:26 集合竞价

`auction-finalize` 在 09:26 执行，前五名生成 `preopen_decisions`：

- `conditional_buy`：公告质检通过，但仍需 09:35 开盘确认
- `watch`：数据或公告扫描不完整，只能关注
- `avoid`：不可成交、澄清公告或硬风险

每条记录包含买入区间、最高追价、止损、两级目标、仓位、T+1 最早卖出日和
失效条件，以及缠论/Serenity 研究证据和组合集中度检查。前五名股票及其板块自动写入
动态监控注册表，有两交易日有效期。

## 09:35 开盘确认

`open-confirmation` 通过 HTTPS 重新拉取实时行情，并为竞价短名单补充截至 09:35 的
分钟线，再并发查询巨潮资讯公告。09:30 到 09:35 只有 5 分钟经过时间（6 个分钟
时间点），因此这是早盘观察关口，不会把未满窗口的数据标成 15/30 分钟回撤：

- `buy`：可成交、价格窗口合格、公告质检通过
- `watch`：条件不足或公告源不可用
- `avoid`：一字板/停牌/涨停不可买，或公告出现澄清、监管、财务硬风险

09:29 的 `portfolio-risk-precompute` 只使用决策日前的日线，为竞价短名单生成相关性、
沪深 300 beta、拟新增后的行业暴露、ADV 容量和组合波动证据。缺字段或覆盖不足仍然
fail-closed，但记为 `blocked -> watch/零仓位`；只有已经测得的集中度或因子超限才记为
`rejected -> avoid/零仓位`。

报告同时保留 `open_score_raw` 与 `open_score_live`。前者用于解释研究模型，后者才可
参与实时排名；未注册趋势策略的实时权重仍为零，修复数据链不会绕过研究闸门。

结果自动写入 `recommendations.json`，同一交易日和股票使用确定性 ID，重跑不会
产生重复推荐。

09:37 的研究专用模拟账户只消费上述正式开盘推荐。它先要求推荐和开盘确认通过，再用
Chanlun 看多结构作二次硬门控；Chanlun 不产生候选、不改变排序、不增加推荐分。满足全部
条件后才按 09:35 之后的可观察行情模拟成交。账户初始资金10万元，独立于真实
`portfolio.json`，完整协议见 [推荐后 Chanlun 门控模拟交易协议](paper-trading-protocol.md)。

报告不再只展示混合总分。每个候选同时展示市场时点、主线板块排名/持续状态、板块内龙头排名、策略研究标识和策略门禁后的真实动作。新游资策略 `daban:mainline_leader_confirm` 在未通过 OOS 注册前只能进入研究观察，仓位为零。

## 09:50 与 13:15 研究确认

两个任务各做一次有界 HTTPS 行情与分钟线刷新。09:50 优先读取当日
`open_confirmation`；即使 09:35 为 `degraded/insufficient_data`，也会依次尝试
`evaluated_confirmations`、`confirmations`，最后回退到同日竞价短名单，因此不会被
09:35 的数据不足连带静默阻断：

- 09:50：只使用截至 09:50 的分钟线，检查开盘后承接和板块内相对强度
- 13:15：检查午后回流和板块内相对强度
- 输出仅为 `confirmed/watch/invalidated` 研究状态
- 不新增订单，不自动买入，不建议当日卖出
- 结果和不可变输入快照写入候选生命周期，供 T+1/T+3 归因

该 lane 的完整判定口径属内部研究文档，不在公开仓库内；此处只描述它对生命周期的
可观测影响（研究状态、不下单、写入不可变快照）。

## 公告质检

质检识别以下信号并阻止正向关键词误加分：

- 澄清、不属实、未涉及、无相关业务
- 尚未形成收入、对业绩无重大影响
- 股票交易异常波动、风险提示
- 立案调查、退市风险、监管问询、资金占用、违规担保、减持

公告扫描失败时状态为 `conditional`，不能输出无条件买入。公告只检查最近 30 天，
防止历史异动公告永久封禁股票。

## 动态监控

盘中监控不再使用硬编码股票列表，观察集合由当前持仓和
`monitor_registry.json` 合并生成。

```bash
python skills/stock-triage/scripts/monitor_manager.py --list
python skills/stock-triage/scripts/monitor_manager.py --add-stock 600519 贵州茅台
python skills/stock-triage/scripts/monitor_manager.py --add-theme AI算力
python skills/stock-triage/scripts/monitor_manager.py --cancel-stock 600519
python skills/stock-triage/scripts/monitor_manager.py --cancel-theme AI算力
```

用户明确取消会写入 `manual_cancelled` 墓碑。普通定时任务不能重新激活；只有新的
手工订阅或真实买入可以显式覆盖。清仓后股票监控自动转为 `closed`。

资讯监控使用固定宏观查询加动态股票/板块/主题查询。股票查询会额外包含公告、
澄清、风险提示和监管问询，已取消主题不会继续推送。

## 全球市场输出

全球监控除原始信息外，新增：

- `sector_views`：A 股板块方向、影响分、置信度和证据
- `stock_watchlist`：代表性股票观察映射

全球映射只允许生成 `watch_only_pending_stock_qc`，不能绕过个股公告和可成交性
质检直接升级为买入建议。

## 自动结算与反馈

交易日收盘后的 `performance-daily` 持续推进所有未 final 的信号：

- T+1：写 `signal.t1_settled`，仅作 provisional 观察
- T+3：写 `signal.t3_settled`，作为 final 绩效结果
- 周度：按最终期望值更新 `strategy_registry.json`，负期望策略可被统一 Policy 停用

人工修正仍追加新事件，不覆盖历史事件。
