# 运行门禁、统一账本与数据访问层

本轮改造把三个原本分散在脚本约定中的架构能力变成确定性代码：

1. cron 任务按 A 股交易日组成运行批次，并在启动业务子进程前验证依赖。
2. 推荐、信号、拟议交易、真实成交、监控状态和 T+1 结算写入同一事件账本。
3. 外部 HTTP 请求统一经过共享 transport/provider，关键阈值从集中配置读取。

## 运行批次与依赖门禁

每个 artifact 使用 `hermes_cron_artifact_v2`，包含：

- `trading_date`：任务所属的最近 A 股交易日。
- `batch_id`：格式为 `a-share-YYYYMMDD`。
- `dependency_gate`：逐项记录上游状态、交易日、年龄和阻断原因。
- `status=blocked`：必需依赖缺失、失败、过期、日期不匹配或批次不匹配。

`blocked` 时 runner 会写 artifact 和 `job_runs.json`，返回码为 `75`，但不会启动
`run.command`。这保证下游不会在上游数据不完整时继续生成看似正常的报告。

manifest 的 `dependency_policy` 支持：

- `trading_date`: `same_trading_date`、`same_batch`、`previous_trading_day` 或 `latest`
- `max_age_minutes`
- `optional_jobs`
- `accepted_statuses`

`validate_cron_manifest.py` 同时检查未知依赖和同批次依赖环。跨交易日边不参与
同批次环检测，例如次日竞价读取前一交易日候选池。

## 统一信号账本

规范账本为：

```text
$A_STOCK_STATE_HOME/skills/stock-triage/data/signal_ledger.jsonl
```

如果未设置 `A_STOCK_STATE_HOME`，路径回退到 `HERMES_HOME` 或 `~/.hermes`。

账本为 append-only JSONL，每个事件有稳定 `event_id`，并通过以下 ID 关联：

- `correlation_id`
- `recommendation_id`
- `signal_id`
- `trade_id`
- `monitor_id`
- `settlement_id`

事件类型包括：

- `recommendation.created`
- `signal.opened`
- `trade.proposed`
- `trade.executed`
- `monitor.activated` / `monitor.deactivated` / `monitor.cancelled` / `monitor.closed`
- `signal.settled`

只有公告与交易质检通过的 `buy/add` 推荐才生成 `signal.opened`。`hold/watch/avoid`
不会进入绩效样本。`trade.proposed` 不表示成交；只有 `portfolio_manager` 实际录入
开仓、加仓或清仓后才写 `trade.executed`。

旧文件继续保留：

- `recommendations.json`
- `trade_history.json`
- `signal_history.json`
- `monitor_registry.json`

它们是兼容视图和既有部署数据，不再是跨模块关联的唯一事实源。绩效统计优先投影
`signal_ledger.jsonl`，再合并尚未迁移的旧 `signal_history.json` 记录。

周度绩效任务现在执行：

```bash
python skills/stock-triage/scripts/performance_tracker.py --json --gate
```

因此结算完成后会自动按策略期望值更新 `strategy_registry.json`。

## 数据 Provider 与配置

共享入口：

- `skills/common/http_client.py`：bytes/text/json、最多两次尝试、类型化错误、抓取时间。
- `skills/common/data_provider.py`：腾讯行情、SerpAPI 等 provider adapter。
- `skills/common/a_stock_http.py`：A 股历史兼容 API，复用同一异常类型。
- `config/data_access.json`：provider、持仓风控、盘中监控、资讯监控，以及全球市场
  数据源开关、标的池、传导阈值和 A 股观察映射。

配置读取失败时使用代码内历史默认值，避免旧部署因缺少新配置文件直接中断。
配置整段或字段类型损坏时也会回退到安全默认值。全球监控的 Yahoo 主源失败时，
会按同一配置启用新浪指数备用源；备用源不足时仍保持 fail-closed，不输出方向判断。

业务脚本不得直接调用 `urllib.request.urlopen`。新增数据源时先在共享 transport/provider
实现 adapter，再由业务脚本调用。

## 部署检查

```bash
python scripts/validate_cron_manifest.py cron/hermes-cron-manifest.json
pytest -q
python scripts/smoke_test.py
```

Hermes 与 OpenClaw 必须设置相同的 `A_STOCK_STATE_HOME`，否则会各自生成账本和监控状态，
闭环仍会被拆成两套。
