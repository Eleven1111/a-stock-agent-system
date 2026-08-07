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
| 作业来源 | `cron/hermes-cron-manifest.json`（60 个作业，当前 45 个 enabled） |
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
| 推送 | `deliver: feishu_direct`；全绿时无输出，`silent_when_no_signal` 直接静默 |
| 停止 | manifest 里把该作业 `enabled` 改为 `false`（dispatcher 下一次心跳即生效） |

存在理由：本机调度器 `Popen` fire-and-forget 起作业，**没有任何消费者读退出码**，
所以链路失败在本机原本完全不可见（issue #159）。它读当日竞价链 5 个作业最新
artifact 的 `status`，并把 `missing`（当日无 artifact，多半是 Mac 睡眠错过 launchd
心跳）与 `failed/timeout/blocked`（跑了但没跑通）分开报 —— 两者运维动作不同。

未配置 `A_STOCK_FEISHU_CHAT_ID` 时 `feishu_push` 返回 `not_configured`，作业照常
跑完、trace 记一条 `delivery.failed not_configured`，不报错也不重试。手动跑：

```bash
A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc PYTHONPATH=skills/common \
  .venv/bin/python scripts/cron_failure_watch.py --json
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

### 历史与注意事项

- 2026-07-20 之前，该 launchd 代理指向的是另一个仓库 `a-stock-agent-claude-code`。本仓库虽然维护着完整 manifest，但缺 `scripts/cron_dispatch.py`，因此本仓库的改动（含 PR #114 / #117）从未在生产生效。当日已将 `cd` 路径切至本仓库并补上 dispatcher（PR #119）。
- `A_STOCK_RUNTIME` 必须为 `hermes`。切换当日曾沿用旧值 `claude-code`，导致 `run_agent_dag.py` 直接失败——该值是另一个仓库的运行模式。
- dispatcher 从工作区直接运行，**不经过构建**。因此工作区里未提交的改动会立即进入生产，务必保持工作区与 main 一致。
