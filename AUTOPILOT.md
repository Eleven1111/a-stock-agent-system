# AUTOPILOT — 后台常驻任务登记

本仓库运行的所有后台/定时任务在此登记。新增任务必须同步登记，否则无法追溯"谁在跑、怎么停"。

## com.a-stock-cc.scheduler（launchd 用户代理）

| 项 | 值 |
|---|---|
| 名称 | `com.a-stock-cc.scheduler` |
| 配置 | `~/Library/LaunchAgents/com.a-stock-cc.scheduler.plist` |
| 心跳 | 每 60 秒（`StartInterval`）唤醒一次 |
| 执行 | `cd <本仓库> && PYTHONPATH=skills/common .venv/bin/python scripts/cron_dispatch.py` |
| 作业来源 | `cron/hermes-cron-manifest.json`（49 个作业，当前 42 个 enabled） |
| 状态根 | `A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc` |
| 运行模式 | `A_STOCK_RUNTIME=hermes` |
| 调度器日志 | `$A_STOCK_STATE_HOME/cron/scheduler.{out,err}.log` |
| 作业输出日志 | `$A_STOCK_STATE_HOME/cron/dispatch-jobs.log` |
| 去重状态 | `$A_STOCK_STATE_HOME/cron/dispatch_state.json` |

**停止：**

```bash
launchctl unload ~/Library/LaunchAgents/com.a-stock-cc.scheduler.plist
```

**确认已停：** `launchctl list | grep a-stock` 无输出即已停止。

**重新启动：**

```bash
launchctl load ~/Library/LaunchAgents/com.a-stock-cc.scheduler.plist
```

### 历史与注意事项

- 2026-07-20 之前，该 launchd 代理指向的是另一个仓库 `a-stock-agent-claude-code`。本仓库虽然维护着完整 manifest，但缺 `scripts/cron_dispatch.py`，因此本仓库的改动（含 PR #114 / #117）从未在生产生效。当日已将 `cd` 路径切至本仓库并补上 dispatcher（PR #119）。
- `A_STOCK_RUNTIME` 必须为 `hermes`。切换当日曾沿用旧值 `claude-code`，导致 `run_agent_dag.py` 直接失败——该值是另一个仓库的运行模式。
- dispatcher 从工作区直接运行，**不经过构建**。因此工作区里未提交的改动会立即进入生产，务必保持工作区与 main 一致。
