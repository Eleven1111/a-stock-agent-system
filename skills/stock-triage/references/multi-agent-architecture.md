# A股全栈 Agent 系统架构

## 四层架构

```
L1: CRON PIPELINE (持续数据采集)
  08:30  BuilderPulse
  08:55  PulseEngine
  10:00  高温开盘跟踪
  11:35  午盘复盘 → 11:40 Triage
  15:05  封测跟踪
  15:15  收板复盘 → 15:25 Triage
  (每5分)价格提醒监控

L2: TRIAGE ORCHESTRATOR (stock-triage profile)
  接收 cron context → 信号检测 → 评分
  ≥8(S) → 4worker全链+Serenity
  6-7(A) → 3worker
  <6    → [SILENT]

L3: KANBAN WORKER FLEET (并行分析)
  stock-analyst (flash)  — 技术面
  hotmoney-worker (flash) — 情绪+催化
  serenity-worker (pro)   — 深度投研

L4: DELIVERY (分发)
  S级 → Discord即时推送
  A级 → 每日简报附带
```

## Profiles 一览

| Profile | 包装名 | 模型 | 模型费 |
|---------|--------|------|--------|
| default | `hermes` | deepseek-v4-flash (OR) | 便宜 |
| stock-data | `stock-data` | deepseek-v4-flash (OR) | 便宜 |
| stock-analyst | `stock-analyst` | deepseek-v4-flash (OR) | 便宜 |
| hotmoney-worker | `hotmoney-worker` | deepseek-v4-flash (OR) | 便宜 |
| serenity-worker | `serenity-worker` | deepseek/deepseek-v4-pro (OR) | 贵 |
| stock-triage | `stock-triage` | deepseek-v4-flash (OR) | 便宜 |

所有 profile 共用 `~/.hermes/.env`（通过软链接），都走 `OPENROUTER_API_KEY`。
如果要用官方 DeepSeek API，改 `config.yaml` 的 `model.provider: deepseek`。

## 信号评分规则

| 信号 | 分值 | 触发条件 |
|------|------|---------|
| 政策/资讯命中跟踪板块 | 10 | 关键词命中 |
| 用户标的+技术金叉/突破 | 9 | 用户列表+技术信号 |
| 连板≥3+封板≥1亿 | 8 | 涨停板数据 |
| 板块热度>10+涨停≥3 | 7 | sector_scan输出 |
| 用户标的+跌超15% | 7 | 跟踪列表跌幅 |
| 首板+封板≥5亿 | 6 | 资金异动 |
| 资金轮动>50亿切换 | 6 | 板块资金流 |
| 3家券商同日覆盖 | 5 | 研报检测 |
| 北向连续3日加仓TOP5 | 5 | 北向数据 |
| 换手>20%+涨>5% | 4 | 异常放量 |

## 快捷指令

在 Discord 输入这些指令时，Luna 会加载 `a-stock-commands` skill 并路由到 Triage：

- `/deep <代码/名称>` — 全链深度分析（4worker + Serenity）
- `/scan <板块>` — 快速板块扫描
- `/alert <代码> <条件>` — 价格提醒
- `/report <板块> <周期>` — 汇总报告
- `/compare <A> <B> [C]` — 横向对比
- `/push` — 立即推送所有待发报告

## 创建时间 / 修改

- 系统创建: 2026-06-02
- stock-triage skill v1.0.0
- stock-analyst skill v3.0.0 (加入多Agent支持)
- a-stock-commands skill v1.0.0
- serenity-investment-research skill v1.0.0 (从 GitHub 安装)
