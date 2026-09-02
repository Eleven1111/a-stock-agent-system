# 推荐后 Chanlun 门控模拟交易协议

## 定位

该模块用于积累可审计的账户级研究数据，不连接券商、不发送真实订单、不改变正式推荐
排序，也不改变 `strategy_registry`。初始模拟资金为 100,000 元。

不可变的入场顺序是：

```text
09:35 正式开盘推荐通过
  -> 开盘确认、公告、数据质量和可成交性通过
  -> Chanlun 看多结构二次门控
  -> 模拟账户纪律和集中度检查
  -> 09:35 后可观察价格模拟成交
```

Chanlun 不是第二套选股策略。它不能生成候选、改变排名或增加推荐分；不在当天
`open_confirmation_v3.signals` 中的股票，即使存在 Chanlun 看多结构，也不会进入模拟
账户。当前 Chanlun 信号仍为 display-only，只允许在该研究账户中作为实验过滤条件。

## 入场门控

配置源为 `config/paper_trading.json`。本协议固定为 `paper_only`：运行报告使用 `paper_live` 标识，绝不等同于真实 `live`。候选必须同时满足：

- 当日 `open_confirmation_v3` 的最终 `decision` 为 `buy/add/conditional_buy`；
- `open_score >= 80`，公告质检通过，执行检查已完成；
- 最近三个已完成日K内存在 `third_buy` 或 `bottom_divergence`；
- 不存在时间更晚的 `third_sell` 或 `top_divergence`；
- 模拟账户未触发周交易次数、单日/单周亏损或连续亏损熔断；
- 非停牌、非涨停不可观察排队，价格未超过原推荐最高追价线。

信号没有 `signal_age_bars`、行情过期、输入快照不可验证或任何必需字段缺失时均拒绝，
不能把缺失数据当成中性证据。

## 成交与账户

- 09:35 完成确认，因此禁止回填为 09:30 开盘价成交；
- 买入使用确认后最新可观察价，加配置滑点；卖出反向扣除滑点；
- 100 股整数手、可用现金、5%现金缓冲、最多5只持仓；
- 仓位沿用推荐的 `execution_plan.position_pct`，不因 Chanlun 额外放大；
- 单票和板块集中度复用正式 `portfolio_policy`；
- 费用复用 `execution_model` 的佣金、最低佣金、印花税和过户费；
- 同一股票已有模拟持仓时不重复加仓，以保持单次推荐归因清晰。

模拟账户投影位于：

```text
$A_STOCK_STATE_HOME/skills/paper-trading/data/paper_portfolio.json
$A_STOCK_STATE_HOME/skills/paper-trading/data/paper_nav_latest.json
```

它们都是可重建投影。规范记录仍写入统一 `signal_ledger.jsonl`，并只使用
`paper_account_after`，不会被真实账户的 `portfolio_after` 投影消费。

## 退出纪律

模拟持仓复用当前配置和原推荐价格计划：

1. 原推荐止损价或全局硬止损；
2. 从持仓最高价回撤的移动止盈；
3. 原推荐目标价或全局止盈；
4. 打板仓位交易日时间止损。

卖出必须通过 A 股 T+1。当天触发退出时记为 `pending_t1`；到下一交易日后，只有正常
可成交才模拟卖出。停牌、跌停或行情缺失继续保留待卖状态，不虚构成交。

## 审计事件

每个进入开盘推荐结果的候选都会产生审计记录，包括未买入的股票：

```text
paper.candidate_evaluated
paper.order.rejected
paper.trade.filled
paper.exit.pending_t1
paper.exit.unfilled
paper.trade.closed
paper.daily_nav
```

事件记录推荐分、推荐决策、Chanlun形态和年龄、接受/拒绝原因、输入快照、信号价、
成交价、滑点、费用、数量、现金、T+1状态和账户快照。确定性幂等键保证任务重跑不会
重复成交；投影丢失时从统一账本恢复。

## 调度

调度源仍是 `cron/hermes-cron-manifest.json`：

- `paper-trading-open`：09:37，强依赖 `open-confirmation`，必须显式传入 `--paper-live`；
- `strategy-promotion-nightly`：夜间可在证据门禁（含 `broker_status=reconciled`）满足时逐级推进至 `manual_pilot`，但仅写入 `mode=paper_only`、`paper_runtime_allowed=true`；真实 `runtime_allowed` 仍为 false，不进入真实 `live`；
- `paper-trading-monitor`：10:08-11:53、13:08-14:53每15分钟错峰检查；
- `paper-trading-close`：15:25，记录收盘净值。

所有入口都通过 `scripts/run_agent_dag.py`，任务仅本地落盘且无外部交易副作用。

## 研究报告

```bash
python scripts/paper_trading_report.py
python scripts/paper_trading_report.py --start 2026-07-01 --end 2026-09-30
```

报告输出 Chanlun门控通过率、拒绝原因、成交数、已平仓胜率、已实现盈亏、净值收益和
最大回撤。没有有效净值历史时返回 `insufficient_data`，不能宣称策略有效。
