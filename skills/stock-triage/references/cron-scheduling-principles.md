# Cron 调度设计原则

> 基于 A 股全栈系统 manifest cron job 与外部 Hermes cron 的实战经验总结

## 时间分散原则

```
❌ 整点扎堆     → 三个 job 都在 09:00
✅ 分散错开     → 08:15 / 08:30 / 08:55 逐步递进
```

- 相邻 job 至少间隔 3 分钟，给上一个留足完成时间
- 避免整点（如 09:00），用 08:55、09:05 等偏移
- 同一分钟内的 job 不要超过 2 个

## 上下文链路设计

收盘 Triage 链：
```
14:30 资金流向 → 15:08 四维打分 → 15:10 持仓风控 → 15:25 Triage
                                      ↑ context_from: [四维打分, 持仓风控, 资金流向]
```

- `context_from` 只注入上游 job 的最近 artifact 摘要，不读取主线用户对话
- 上游 job 的输出必须结构化（JSON 或固定格式 markdown）以便 runner 生成 summary
- 链路深度建议 ≤3 级，避免传递衰减

## 静默式 vs 定时式

| 类型 | 示例 | 策略 |
|------|------|------|
| 静默式 | 盘中异动(5min)、资讯监控 | 无触发完全不输出，避免刷屏 |
| 定时式 | 收盘复盘、盘前扫描 | 每次都输出，但控制篇幅在一屏内 |

静默式 job 的核心逻辑：
```python
if not alerts:
    exit(0)  # 不输出任何内容
```

## 推送分层

```
🔴 紧急 → deliver: origin（立即推送到 Discord）
🟡 重要 → deliver: origin（但 prompt 要求压缩到一屏）
🟢 常规 → deliver: origin（完整报告）
⚪ 数据 → deliver: local（仅本地存档，不推送）
```

当前分配：
- 🔴：盘中异动、持仓风控（止损触发时）
- 🟡：资讯监控、资金流向、四维打分、Triage
- 🟢：全球扫描、收盘复盘、运行时主题
- ⚪：机构追踪、事件日历、胜率统计

## 数据采集模式

仓库 manifest 中的 cron job 统一使用 `hermes_job_runner`：
```json
{
  "command": "python scripts/hermes_job_runner.py capital-flow",
  "context_scope": "cron",
  "deliver": "origin",
  "context_from": ["intraday-alert"],
  "artifact_path_template": "{cron_output_dir}/{job_id}/{run_id}.json",
  "run": {
    "command": "python skills/stock-triage/scripts/capital_flow_monitor.py --json",
    "timeout_seconds": 60
  }
}
```

runner 负责：
- 启动隔离子进程执行 `run.command`
- 写 `$HERMES_HOME/cron/output/{job_id}/{run_id}.json`
- 写 `$HERMES_HOME/cron/output/job_runs.json`
- 根据 `deliver` / `silent_when_no_signal` / `max_output_chars` 控制主线输出

Agent prompt 型 cron 才使用 `delegate_task(...)`；主 cron agent 不直接抓数据、不直接重计算。
**禁止**在 cron prompt 中直接使用 `terminal` 工具（会触发审批锁）。

## API 依赖设计

| 依赖级别 | 示例 | 失败处理 |
|---------|------|---------|
| 无依赖 | 腾讯 qt.gtimg.cn、yfinance | 直接报错，job 标记 failed |
| 软依赖 | 东财 push2（需 NO_PROXY） | 降级到腾讯/新浪替代数据 |
| 可选依赖 | SerpAPI（需 API key） | 跳过新闻模块，其他数据继续 |

东财 push2 API 的特殊性：只在 Hermes agent 主进程中可通过 NO_PROXY 访问（终端直连被 Clash TUN 拦截）。cron job 运行在 agent 上下文中，所以正常。
