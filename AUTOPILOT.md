# AUTOPILOT — 后台常驻任务登记

本仓库运行的所有后台/定时任务在此登记。新增任务必须同步登记，否则无法追溯"谁在跑、怎么停"。

## com.a-stock-cc.scheduler（launchd 用户代理）

> **状态：2026-08-07 19:11 已停止并 disable**（用户要求）。生产在部署机一侧，本机这个实例
> 只产出候选/研究流水，`portfolio.json` 为空账户。停止方式见下方"停止"，已额外执行
> `launchctl disable` 防止下次登录自动加载；plist 与 `~/.a-stock-agent-cc` 数据均未删除。
> 重启需要先 `launchctl enable gui/$(id -u)/com.a-stock-cc.scheduler` 再 load。

| 项 | 值 |
|---|---|
| 名称 | `com.a-stock-cc.scheduler` |
| 配置 | `~/Library/LaunchAgents/com.a-stock-cc.scheduler.plist` |
| 心跳 | 每 60 秒（`StartInterval`）唤醒一次 |
| 执行 | `cd <本仓库> && PYTHONPATH=skills/common .venv/bin/python scripts/cron_dispatch.py` |
| 作业来源 | `cron/hermes-cron-manifest.json`（66 个作业，当前 52 个 enabled） |
| 状态根 | `A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc` |
| 运行模式 | `A_STOCK_RUNTIME=hermes` |
| 调度器日志 | `$A_STOCK_STATE_HOME/cron/scheduler.{out,err}.log` |
| 作业输出日志 | `$A_STOCK_STATE_HOME/cron/dispatch-jobs.log` |
| 去重状态 | `$A_STOCK_STATE_HOME/cron/dispatch_state.json` |
| 执行事件流 | `$A_STOCK_STATE_HOME/cron/execution_trace.jsonl`（只追加，shadow 观测） |

**停止：**

```bash
launchctl unload ~/Library/LaunchAgents/com.a-stock-cc.scheduler.plist
launchctl disable gui/$(id -u)/com.a-stock-cc.scheduler
```

**确认已停：** `launchctl list | grep a-stock` 无输出；再等两个心跳（>120 秒）复查
`~/.a-stock-agent-cc/cron/output/job_runs.json` 时间戳不再前进。注意 plist 带
`AbandonProcessGroup=true`，已 detach 的子任务会跑完，不会随 unload 被杀。

**重新启动：**

```bash
launchctl enable gui/$(id -u)/com.a-stock-cc.scheduler
launchctl load ~/Library/LaunchAgents/com.a-stock-cc.scheduler.plist
```

### 诊断与回滚

```bash
# 一次运行的完整重建 + 全 manifest 覆盖检查
python scripts/execution_trace_report.py --coverage
```

关注三项：`trace_gaps` 必须为空；`shadow_gate` 三项必须全 true；`duration_seconds.p95`
相对接入 trace 前的基线不得回归超过 5%。连续五个交易日满足这三项，才算通过 Shadow 门。

回滚开关（作业行为不受影响，只停止写 trace）：

```bash
# 在 launchd plist 的 EnvironmentVariables 中加入，然后 unload / load
A_STOCK_EXECUTION_TRACE=off
```

**不要**为了回滚删除已经写入的 `execution_trace.jsonl` 或 `signal_ledger.jsonl`。

类型化命令：dispatcher 只接受 `command_argv` 数组并以 `shell=False` 执行。作业启动失败时
先看 `dispatch-jobs.log` 与调度器 err 日志里的 `skip job ...` 行 —— 可执行文件缺失、cwd
越界、argv 里出现 shell 元字符都会 fail closed 并记录原因，不会静默降级回 shell。

### 集合竞价链路失败汇总（auction-chain-watch）

| 项 | 值 |
|---|---|
| id | `auction-chain-watch`（`enabled: true`） |
| 调度 | `35 9,10 * * 1-5`（09:35 竞价链应已收口，10:35 复查做双保险） |
| 执行 | `python scripts/cron_failure_watch.py`（只读 artifact，不写业务状态） |
| 推送 | `deliver: local`（2026-08-19 起飞书推送全仓停用，见下）；全绿时无输出，`silent_when_no_signal` 直接静默 |
| 停止 | manifest 里把该作业 `enabled` 改为 `false`（dispatcher 下一次心跳即生效） |

存在理由：本机调度器 `Popen` fire-and-forget 起作业，**没有任何消费者读退出码**，
所以链路失败在本机原本完全不可见（issue #159）。它读当日竞价链 5 个作业最新
artifact 的 `status`，并把 `missing`（当日无 artifact，多半是 Mac 睡眠错过 launchd
心跳）与 `failed/timeout/blocked`（跑了但没跑通）分开报 —— 两者运维动作不同。

手动跑：

```bash
A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc PYTHONPATH=skills/common \
  .venv/bin/python scripts/cron_failure_watch.py --json
```

### 飞书推送全仓停用（2026-08-19）

原先 7 个 `deliver: feishu_direct` 的作业（`auction-chain-watch`、`capital-flow`、
`event-calendar`、`news-monitor`、`news-monitor-intraday`、`news-monitor-weekend`、
`official-policy-watch`）已统一改为 `deliver: local`：**manifest 里现在没有任何
飞书推送作业**，告警只落盘不外发，需要主动看 artifact 或诊断包。

恢复方式：把对应作业的 `deliver` 改回 `feishu_direct` 并配置 `A_STOCK_FEISHU_CHAT_ID`
（未配置时 `feishu_push` 返回 `not_configured`，作业照常跑完、trace 记一条
`delivery.failed not_configured`，不报错也不重试）。

### 每日运行诊断包归档（daily-diagnostics）

| 项 | 值 |
|---|---|
| id | `daily-diagnostics`（`enabled: true`） |
| 调度 | `10 23 * * *`，`trading_day_policy: calendar_day`（**非交易日也跑**） |
| 执行 | `python scripts/daily_diagnostics.py --archive`（纯只读聚合） |
| 产出 | `$A_STOCK_STATE_HOME/diagnostics/<日期>.md`，**滚动保留 30 天** |
| 推送 | `deliver: local`；回给调度器的只有一行摘要（报告本体在磁盘上） |
| 停止 | manifest 里把该作业 `enabled` 改为 `false`（dispatcher 下一次心跳即生效） |

存在理由：系统跑在两个互相看不见的 Agent 里（OpenClaw 网关 + Hermes 调度器），
排障证据散落五处。**事故当天再去翻往往已经没了** —— OpenClaw 主日志默认在
`/tmp/openclaw/`，macOS 重启即清空。每天先存一份，出事时直接把这个文件发出去。

**非交易日也跑是刻意的**：调度器整体停摆这种故障，恰恰只能从「非交易日也没有
报告」看出来；只在交易日跑的话，周末停摆到周一才会暴露。

报告已按规则脱敏（`sk-`/`ghp_`/`xox*`/Bearer/`*key|token|secret|chat_id=`/长十六进制），
但**传出前请自行再扫一眼**。清理只认 `YYYY-MM-DD.md` 命名，手工另存的事故存档
（如 `2026-08-06-incident.md`）不会被删。

手动跑：

```bash
python scripts/daily_diagnostics.py --out ~/Desktop/diag.md          # 临时看一份
python scripts/daily_diagnostics.py --date 2026-08-06 --archive      # 补归档某一天
```

### 待启用：公告召回雷达（announcement-radar）

三个作业已登记进 manifest，但**当前 `enabled: false`**，处于待观察状态：

| id | schedule | 说明 |
|---|---|---|
| `announcement-radar-evening` | `30 21 * * 1-5` | 主跑，抓当日全市场公告 |
| `announcement-radar-premarket` | `40 7 * * 1-5` | 补抓凌晨增量，赶在 08:30 候选池引导前 |
| `announcement-radar-weekend` | `0 9 * * 6` | 周六跑（实测 08-01 周六仍有 1440 条） |

启用前需连续 5 个交易日手动跑并满足：抓取量 > 1000、分类命中率 ≥ 85%、
召回 100~250 条、`cninfo` 源既有调用失败率无上升。手动跑：

```bash
A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc PYTHONPATH=skills/common \
  .venv/bin/python skills/announcement-radar/scripts/radar.py --date today --emit-brief
```

**耗时 6~14 分钟**（2026-08-04 两次实测：5:59 与约 13 分钟，网络波动导致差异大）：
全市场约 50 次接口请求 × `http_client` 2.5s/源 节流。
因此 `run.timeout_seconds` 设为 1800，不可套用其他作业的 15~60s。
产物：`$A_STOCK_STATE_HOME/cron/output/announcement-radar/<date>.json`。

### 研究数据集物化（research-dataset-build）

把 catalog 声明的数据集物化成契约行，让研究数据真正开始积累。此前 catalog
只有契约、没有生产方，没有任何数据集在增长。

| 项 | 值 |
|---|---|
| 作业 id | `research-dataset-build` |
| 调度 | `40 16 * * 1-5`（排在结算作业 `performance-daily` 16:10 之后） |
| 执行 | `python scripts/build_research_datasets.py --json` |
| 依赖 | `performance-daily`（同交易日，最大延迟 120 分钟） |
| 交付 | `local`（只写本地产物，不推送） |
| 产物 | `$A_STOCK_STATE_HOME/skills/stock-triage/data/dataset_<dataset_id>_<asof>.json` |
| 超时 | 120s（`standard` 档；取行情的方向数据集走并发，4 worker） |

**停止：** 把 manifest 中该作业的 `enabled` 改为 `false`，或停整个调度器（见上）。

```bash
python -c "import json;p='cron/hermes-cron-manifest.json';d=json.load(open(p));[j.update(enabled=False) for j in d['jobs'] if j['id']=='research-dataset-build'];json.dump(d,open(p,'w'),ensure_ascii=False,indent=2)"
```

**手动跑（离线，只建已结算信号数据集，不触网）：**

```bash
A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc PYTHONPATH=skills/common \
  .venv/bin/python scripts/build_research_datasets.py --settled-only --json
```

注意：`coverage_ratio` 衡量的是**投影完整性**（多少源记录成功变成行），
不是样本是否足够。3 条结算样本同样会得到 1.0。样本量守卫在下游
（`cross_sectional_direction` 的 `min_pairs_per_cohort = 100`）。

### 收盘历史日K缓存更新（market-history-cache）

给竞价链和回测提供本地日线底仓：akshare 免费源只回溯最近 3~4 周，前收/前一日量额
缺失会让竞价量比算不出来。本作业收盘后按缺失日增量补齐，落成一张本地 SQLite。

| 项 | 值 |
|---|---|
| id | `market-history-cache`（`enabled: true`） |
| 调度 | `10 15 * * 1-5`（收盘后 10 分钟） |
| 执行 | `python scripts/market_history_cache.py --json` |
| 数据源 | BaoStock；**未安装时输出 `status: blocked`**，不报错、不影响其他作业 |
| 产物 | `$A_STOCK_STATE_HOME/market/history.sqlite3` |
| 交付 | `local`（只写本地产物，不推送） |
| 超时 | 300s（`standard` 档；全市场按缺失日增量，单票失败只记 `failed` 不中断批次） |
| 停止 | manifest 里把该作业 `enabled` 改为 `false`（dispatcher 下一次心跳即生效） |

**手动跑（先空跑看范围，再实写）：**

```bash
A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc \
  .venv/bin/python scripts/market_history_cache.py --dry-run --json
```

### 历史与注意事项

- 2026-07-20 之前，该 launchd 代理指向的是另一个仓库 `a-stock-agent-claude-code`。本仓库虽然维护着完整 manifest，但缺 `scripts/cron_dispatch.py`，因此本仓库的改动（含 PR #114 / #117）从未在生产生效。当日已将 `cd` 路径切至本仓库并补上 dispatcher（PR #119）。
- `A_STOCK_RUNTIME` 必须为 `hermes`。切换当日曾沿用旧值 `claude-code`，导致 `run_agent_dag.py` 直接失败——该值是另一个仓库的运行模式。
- dispatcher 从工作区直接运行，**不经过构建**。因此工作区里未提交的改动会立即进入生产，务必保持工作区与 main 一致。
