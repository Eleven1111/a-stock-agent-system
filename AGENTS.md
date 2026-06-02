# A 股全栈 Agent 系统 · AGENTS.md

> 项目宪法。任何 agent 在此项目目录下工作时，必须先读本文。

## 系统身份

这是一个 **A 股投资决策辅助系统**，不是交易机器人。它的职责是：
- 采集数据、分析信号、给出买卖建议
- **不自动下单、不操作账户、不代替人做最终决策**

## 架构

```
                        ┌─────────────────┐
                        │   stock-triage   │  编排中枢
                        │  (决策 + 派发)    │
                        └────────┬────────┘
                                 │
        ┌────────────┬───────────┼───────────┬────────────┐
        ▼            ▼           ▼           ▼            ▼
   stock-analyst  hot-money  global-mkt  news-to-sector  serenity
   (技术分析)     (游资情绪)   (全球监控)   (催化映射)     (深度投研)
        │            │           │           │            │
        └────────────┴───────────┴───────────┴────────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
               capital_flow  portfolio    intraday
               (资金流向)     (持仓风控)    (盘中异动)
```

**技能树（10 个 skill）：**

| Skill | 路径 | 角色 |
|-------|------|------|
| stock-triage | `~/.hermes/skills/stock-triage/` | 🧠 编排中枢 |
| stock-analyst | `~/.hermes/skills/stock-analyst/` | 📊 技术分析引擎 |
| hot-money-tactics | `~/.hermes/skills/hot-money-tactics/` | 🔥 游资情绪 |
| global-market-monitor | `~/.hermes/skills/global-market-monitor/` | 🌍 全球外围 |
| news-to-sector | `~/.hermes/skills/news-to-sector/` | 📡 资讯→板块映射 |
| serenity-investment-research | `~/.hermes/skills/serenity-investment-research/` | 🎓 深度投研 |
| a-stock-data | `~/.hermes/skills/a-stock-data/` | 📦 数据源参考 |
| a-stock-daily-report | `~/.hermes/skills/a-stock-daily-report/` | 📋 每日简报 |
| a-stock-commands | `~/.hermes/skills/a-stock-commands/` | ⌨️ 快捷指令 |
| pulse-engine | `~/.hermes/skills/pulse-engine/` | 📡 社会情绪 |

## 数据源铁律

### ✅ 始终可用（24×7）

| 源 | 端点 | 编码 | 覆盖 |
|----|------|------|------|
| 腾讯实时 | `qt.gtimg.cn/q={market}{code}` | GBK | A股/港股实时行情 |
| 腾讯K线 | `web.ifzq.gtimg.cn/appstock/app/fqkline/get` | JSON | 日/周/月/60/30 K线 |
| 新浪实时 | `hq.sinajs.cn/list={codes}` | GBK | A股实时行情 |
| yfinance | Yahoo Finance | — | 美股/全球指数/期货/VIX/汇率 |

### ⚠️ 需 Hermes Agent 环境（cron 内自动可用，终端需手动加载 .env）

| 源 | 端点 | 需要 |
|----|------|------|
| 东方财富 | `push2his.eastmoney.com` | NO_PROXY=.eastmoney.com |
| 东财数据中心 | `datacenter.eastmoney.com` | NO_PROXY=.eastmoney.com |
| SerpAPI | `serpapi.com` | SERPAPI_API_KEY |

### ❌ 不可用

| 源 | 原因 |
|----|------|
| BaoStock K线 | macOS 12 兼容性问题（备用） |
| 新浪港股 | `hq.sinajs.cn/list=hkXXXXX` 返回 Forbidden |
| 东财 `push2.eastmoney.com`（TUN直连） | Clash Verge DNS 劫持 → 198.18.x.x |

### 编码规则

- **腾讯** 返回 GBK，必须 `.decode("gbk")`
- **新浪** 返回 GBK，必须 `.decode("gbk")`
- **yfinance** 返回标准 Python 对象，UTF-8
- **东财 JSON API** 返回 UTF-8 JSON
- **写入文件/输出** 始终 UTF-8

## 绝对铁律（违反即事故）

### 1. 配置隔离
四个工具配置**绝不串改**：
```
OpenClaw → ~/.openclaw/
Claude Code → ~/.claude/
Codex → ~/.codex/
Hermes → ~/.hermes/
```
**改配置前必须确认目标工具。**

### 2. Cron 数据采集
cron 任务中所有数据抓取**必须用 `execute_code` + Python `urllib`**，
**禁止用 `terminal` 工具**（会触发安全审批锁，导致 cron 卡死）。

### 3. 全量扫描
用户说"全量"时必须真正穷尽，不能只扫预设板块。
参考：`sector_scan.py` 遍历所有行业板块。

### 4. 分析输出
任何个股分析必须包含：
- 具体买入价 / 止损位 / 目标位
- 持有周期
- S/A/B/C 分级 + 仓位建议

只给数据和判断依据，不给"可能/或许/建议关注"等模糊词。

### 5. 网络故障处理
遇到 502 / DNS 劫持 / 连接拒绝等报错时：
- **先排查根因，修好，再汇报结果**
- 不要接连报错让用户处理
- 不要反复重试同一个失败端点（最多 2 次）

### 6. Cron 时间规则
- 不扎堆，时间分散错开
- 避免整点（如 08:30/08:55 而非 09:00）
- 新建 cron 前先 `cronjob(action='list')` 检查现有时间表

## 添加新功能的检查清单

1. **数据源是否在可用列表内？** → 不在则先验证端点
2. **是否需要新的 Python 依赖？** → 先 `pip install` 到 Hermes venv
3. **脚本是否 cron-safe？** → 只用 `urllib`，别用 `requests`（除非加载 `.env`）
4. **Cron 时间是否与现有冲突？** → 查 `cronjob list`
5. **是否需要更新 AGENTS.md？** → 数据源/铁律有变化必须更新
6. **输出是否控制在 Discord 一屏内？** → 太长会被截断
7. **无信号时是否静默？** → 高频 cron（盘中异动、资讯监控）必须静默

## 脚本规范

### 文件位置
```
~/.hermes/skills/{skill-name}/
├── SKILL.md              # 技能文档（每个 skill 必有）
├── scripts/
│   └── {功能名}.py        # 可独立运行
├── references/
│   └── {参考文档}.md
└── data/                  # 运行时数据（自动创建）
    └── {数据文件}.json
```

### Python 脚本头部模板
```python
#!/usr/bin/env python3
"""
{简要描述}

数据源：{列出使用的端点}
Usage:
  python3 {script}.py
  python3 {script}.py --json
"""

import json, sys, os, urllib.request
from datetime import datetime
from typing import Dict, Any, List, Optional

# ========== 数据源 ==========
# {端点 + 编码 + 字段说明}
```

### 输出格式
- `--json` 标志 → 纯 JSON 到 stdout（供下游消费）
- 默认 → 人类可读 Markdown（供 Discord 展示）
- 无信号场景 → 静默退出（return code 0，无 stdout）

## 关键文件索引

| 文件 | 位置 | 用途 |
|------|------|------|
| portfolio.json | `stock-triage/data/` | 持仓数据 |
| signal_history.json | `stock-triage/data/` | 历史信号记录 |
| intraday_alerts.json | `stock-triage/data/` | 盘中告警去重缓存 |
| alerts.json | `~/.hermes/cron/output/` | 价格提醒数据 |
| .env | `~/.hermes/` | API keys + NO_PROXY |

## 用户偏好（来自记忆）

- 中文交流，分析报告用中文
- 深度参与 A 股，关注板块：封测 / AI 算力 / 军工航天 / 电网 / 家电 / 煤炭
- 跟踪标的：华能国际(600011) / 通富微电(002156) / 长电科技(600584) / 华天科技(002185) / 深科技(000021) / 太极实业(600667)
- 要求明确操作建议，不喜欢模糊分析
- 对分析质量要求极高，'全量扫描'必须是真的全量
- 排障时先沿现有认证链路，不要擅自切换 provider
