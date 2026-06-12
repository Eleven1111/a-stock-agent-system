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

## 09:26 集合竞价

`auction-finalize` 在 09:26 执行，前五名生成 `preopen_decisions`：

- `conditional_buy`：公告质检通过，但仍需 09:35 开盘确认
- `watch`：数据或公告扫描不完整，只能关注
- `avoid`：不可成交、澄清公告或硬风险

每条记录包含买入区间、最高追价、止损、两级目标、仓位、T+1 最早卖出日和
失效条件，以及缠论/Serenity 研究证据和组合集中度检查。前五名股票及其板块自动写入
动态监控注册表，有两交易日有效期。

## 09:35 开盘确认

`open-confirmation` 重新拉取实时行情并并发查询巨潮资讯公告：

- `buy`：可成交、价格窗口合格、公告质检通过
- `watch`：条件不足或公告源不可用
- `avoid`：一字板/停牌/涨停不可买，或公告出现澄清、监管、财务硬风险

结果自动写入 `recommendations.json`，同一交易日和股票使用确定性 ID，重跑不会
产生重复推荐。

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
python skills/stock-triage/scripts/monitor_manager.py --add-stock 002156 通富微电
python skills/stock-triage/scripts/monitor_manager.py --add-theme AI算力
python skills/stock-triage/scripts/monitor_manager.py --cancel-stock 002156
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
