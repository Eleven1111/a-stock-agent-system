---
name: a-stock-commands
description: >
  A股快捷指令。当用户使用 /deep /scan /alert /report /compare /push 等指令时自动加载。
  所有指令自动路由到 stock-triage orchestrator 执行。
version: 1.0.0
author: Luna
metadata:
  hermes:
    tags: [A股, 快捷指令, 操作]
    category: finance
---

# A股快捷指令 /slash commands

## 指令参考

### /deep — 深度分析
```
/deep 002156
/deep 通富微电
/deep 002156,600584  (多只)
```
→ 触发 stock-triage orchestrator: 4-worker 全链分析 + Serenity 深度报告

### /scan — 板块扫描
```
/scan 军工
/scan 封测
/scan 全量
```
→ 扫描指定板块 / 全市场。输出候选标的 + 快速技术判断。

### /alert — 价格提醒
```
/alert 600011 止损8.0
/alert 002156 突破70
/alert 华能国际 异动±5%
```
→ 添加到监控池，创建价格触发条件。

### /report — 定期报告
```
/report 封测 本周
/report 高温主题 昨日
/report 全市场 今日
```
→ 汇总指定板块/全市场的表现与展望。

### /compare — 横向对比
```
/compare 通富微电 长电科技 华天科技
```
→ 多只股票横向对比：技术面 + 基本面 + 资金面 + 估值的雷达图式对比。

### /push — 紧急推送
```
/push
```
→ 立即推送当前缓存的所有待发分析报告到 Discord。

### /global — 全球市场扫描 🆕
```
/global
/global --news
```
→ 触发 global-market-monitor，即时输出全球市场全景 + A股影响评估。

## 实现规则

当用户在 Discord 中发送以上指令时：
1. 检测指令并提取参数
2. 加载 stock-triage skill 获取编排逻辑
3. 根据指令类型决定分发策略：

| 指令 | 分发策略 | Worker(s) |
|------|---------|-----------|
| /deep | 全链深度 | stock-analyst → hotmoney → serenity |
| /scan | 快速扫描 | stock-analyst only |
| /global | 全球扫描 | global-market-monitor → news-to-sector |
| /alert | 创建提醒 | 本地文件 + cron monitor |
| /report | 汇总查询 | stock-analyst + hotmoney |
| /compare | 横向对比 | stock-analyst (compare mode) |
| /push | 即时推送 | 发送所有待发报告 |

## 提醒存储格式

```json
// ~/.hermes/cron/output/alerts.json
[
  {"code": "600011", "name": "华能国际", "type": "stop_loss", "price": 8.0, "created": "2026-06-02T19:00+08:00", "active": true},
  {"code": "002156", "name": "通富微电", "type": "breakout", "price": 70.0, "created": "2026-06-02T19:00+08:00", "active": true}
]
```

提醒监控 cron job（每分钟检查）：
```bash
# 检查所有活跃提醒是否触发
python3 scripts/check_alerts.py
```
