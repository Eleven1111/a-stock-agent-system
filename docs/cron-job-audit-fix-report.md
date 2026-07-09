# A-stock Agent System 定时任务审计修复报告

修复日期：2026-07-07

## 修复概览

本次按 `docs/cron-job-audit-report.md` 的优先级修复 `cron/hermes-cron-manifest.json`，重点处理禁用任务被硬依赖、HEARTBEAT 与 manifest 不一致、低价值任务保持非安装状态，以及高频任务降频。Phase 1 行为金融任务 `behavioral-finance-preopen` 和 `behavioral-finance-close` 已保留启用。

## 逐项修复

### 1. 硬依赖冲突

| 任务 | 字段 | 修复前 | 修复后 | 说明 |
| --- | --- | --- | --- | --- |
| `candidate-discovery` | `dependency_policy.optional_jobs` | `["social-attention-close"]` | `["hk-a-linkage", "social-attention-close"]` | `hk-a-linkage` 已禁用，保留上下文引用但改为 optional，避免阻断盘后候选发现。 |
| `portfolio-check` | `dependency_policy.optional_jobs` | 未设置 | `["four-dim-scorer"]` | `four-dim-scorer` 已禁用，保留风控可读证据但不作为硬阻断。 |
| `closing-triage` | `dependency_policy.optional_jobs` | 未设置 | `["four-dim-scorer"]` | 收盘复盘继续消费可用四维评分，缺失时不阻断 `portfolio-check` 和 `capital-flow`。 |

### 2. HEARTBEAT 对齐

| 任务 | 字段 | 修复前 | 修复后 |
| --- | --- | --- | --- |
| `intraday-alert` | `enabled` | `true` | `false` |
| `capital-flow` | `context_from` | `["intraday-alert", "open-confirmation"]` | `["open-confirmation"]` |
| `social-attention-preopen` | `enabled` | `true` | `false` |
| `social-attention-midday` | `enabled` | `true` | `false` |
| `social-attention-close` | `enabled` | `true` | `false` |
| `serenity-refresh-plan` | `enabled` | `true` | `false` |

`behavioral-finance-preopen` 和 `behavioral-finance-close` 保持 `enabled=true`；它们对社会关注度的引用仍为 optional。

### 3. 低价值任务清理

以下任务已保持 `enabled=false`，未删除 manifest 条目，便于保留手工诊断入口和现有清单测试覆盖：

| 任务 | 当前状态 | 处理 |
| --- | --- | --- |
| `provider-health` | `enabled=false` | 保持禁用 |
| `market-pulse-1314` | `enabled=false` | 保持禁用 |
| `market-pulse-1500` | `enabled=false` | 保持禁用 |
| `ledger-projector` | `enabled=false` | 保持禁用 |

### 4. 降频优化

| 任务 | 字段 | 修复前 | 修复后 |
| --- | --- | --- | --- |
| `official-policy-watch` | `schedule` | `3,13,23,33,43,53 8-22 * * *` | `3,33 9-11,13-14 * * 1-5` |
| `official-policy-watch-evening` | 新增任务 | 无 | `3 22 * * 1-5` |
| `news-monitor-intraday` | `schedule` | `2,17,32,47 9-11,13-14 * * 1-5` | `2,32 9-11,13-14 * * 1-5` |
| `global-evening` | `schedule` | `30 22 * * 1-5` | `30 22 * * 0` |
| `stock-intelligence-refresh` | `schedule` | `40 15 * * 1-5` | `40 15 * * 0` |
| `hot-money-afternoon-checkpoint` | `schedule` | `15 13 * * 1-5` | 保持不变 |
| `company-event-opportunity-scan` | `schedule` / `enabled` | `35 8 * * 1-5` / `true` | 保持不变 |
| `catalyst-trigger` | `schedule` / `dependency_policy` | `3,33 9-11,13-14 * * 1-5` / previous trading day 依赖 | 保持不变；未发现禁用硬依赖 |

说明：单个五字段 cron 无法同时表达“盘中每 30 分钟”和“晚间 22 点一次”，因此新增 `official-policy-watch-evening` 复用同一业务脚本，保留晚间低频巡检。

## 配套测试调整

- `tests/test_cron_manifest.py` 同步更新 manifest 契约断言，包括 optional 依赖、禁用社会关注任务、政策/资讯降频和新增晚间政策任务。
- `tests/test_agent_dag.py` 的临时 DAG 环境显式清空继承的 `A_STOCK_STATE_ID`，避免本机 `.hermes` 集群身份配置污染临时 state home；生产运行仍由 cron 环境传入 `A_STOCK_STATE_ID=a-stock-cluster` 并保持 fail-closed。

## 验证结果

| 命令 | 结果 |
| --- | --- |
| `A_STOCK_STATE_HOME=/Users/eleven/.hermes /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 scripts/validate_cron_manifest.py` | 通过：`OK: 49 jobs (0 local, 49 external)` |
| `A_STOCK_STATE_HOME=/Users/eleven/.hermes /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m pytest tests/test_cron_manifest.py tests/test_agent_dag.py tests/test_openclaw_cron_export.py -q` | 通过：`46 passed in 2.14s` |
| `A_STOCK_STATE_HOME=/Users/eleven/.hermes /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 scripts/generate_openclaw_cron.py --reconcile` | 通过：退出码 0；已按 manifest 创建/编辑 OpenClaw cron，包括 `official-policy-watch-evening`。 |

## 未修复项及原因

- 未删除 `provider-health`、`market-pulse-1314`、`market-pulse-1500`、`ledger-projector` 的 manifest 条目：当前保持 `enabled=false` 即不会安装到 OpenClaw cron，同时保留手工运行和测试契约。
- 未启用 `four-dim-scorer`：本次选择将其作为 optional 证据，避免恢复一个已禁用任务带来盘后额外噪音；后续若确认风控强依赖其产出，可单独恢复启用并观察。
- 未改 `catalyst-trigger` 频率：当前已为每 30 分钟，且依赖为 previous trading day 的 `candidate-discovery`，未发现禁用硬依赖。

## 后续建议

- 观察 1 到 2 个交易日的 `cron/output/job_runs.json`，确认高频 `blocked_state` 是否随 OpenClaw cron 同步和 state identity 环境修复消失。
- 若 `four-dim-scorer` 对风控复盘实际价值高，建议恢复为低频收盘 local 任务，或将其产出并入 `closing-triage` 的非阻断证据段。
- 若社会关注度后续重新启用，仍应保持单源只展示、多源覆盖才参与排序的门禁。
