<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/badge/A--Stock-Agent_System-1a1a2e?style=for-the-badge">
  <img alt="A-Stock Agent System" src="https://img.shields.io/badge/A--Stock-Agent_System-ffffff?style=for-the-badge">
</picture>

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-22%20passed-brightgreen)](tests/)
[![Smoke](https://img.shields.io/badge/smoke-6%2F6%20passed-brightgreen)](scripts/smoke_test.py)

A 股多智能体投研系统。9 个专业 Skill、四维打分引擎、覆盖从全球宏观到持仓风控的完整决策链路。

**非交易机器人。** 系统只做数据分析和分级建议，不下单、不操盘。

---

## 架构

```mermaid
graph TD
    TRIAGE[stock-triage<br/>编排中枢] --> ANALYST[stock-analyst<br/>技术分析引擎]
    TRIAGE --> HOTMONEY[hot-money-tactics<br/>游资情绪]
    TRIAGE --> GLOBAL[global-market-monitor<br/>全球宏观]
    TRIAGE --> NEWS[news-to-sector<br/>催化映射]
    TRIAGE --> SERENITY[serenity-investment-research<br/>深度投研]

    ANALYST --> FLOW[capital_flow_monitor<br/>资金流向]
    ANALYST --> PORT[portfolio_manager<br/>持仓风控]
    ANALYST --> INTRA[intraday_monitor<br/>5分钟异动]
    HOTMONEY --> FLOW
    GLOBAL --> FLOW
```

## 能力矩阵

| 模块 | 功能 | 数据源 |
|------|------|--------|
| **stock-analyst** | 日/周/60分/30分多框架技术分析、板块扫描、条件筛选 | 腾讯、新浪、yfinance |
| **hot-money-tactics** | 涨停板全景、连板梯队、封板质量、情绪周期、板块轮动 | AkShare |
| **global-market-monitor** | 美股/VIX/美债/期货/外汇/自然灾害 → A股影响评估 | yfinance、USGS、GDACS |
| **news-to-sector** | 实时资讯→18条产业链映射 + 预期差分析 | SerpAPI |
| **serenity-investment-research** | 深度投研：供应链拆解、财务分析、估值情景、熊市审计 | cninfo、pypdf |
| **four-dim scorer** | S/A/B/C 加权分级：技术(30%)×情绪(25%)×催化(25%)×深度(20%) | 以上全部 |
| **hk-a-linkage** | AH溢价率、恒生背离、港股权重异动 | 腾讯、yfinance |
| **capital-flow-monitor** | 北向资金、主力/散户资金、板块资金 | 东方财富 |
| **portfolio-manager** | 持仓跟踪、止损止盈、回撤止盈、仓位集中度风控 | 腾讯 |
| **intraday-monitor** | 5分钟异动告警：涨跌停、放量、急涨急跌 | 腾讯 |
| **institution-tracker** | 机构调研、券商研报、大股东增减持 | 东方财富 |
| **event-calendar** | 限售解禁、分红除权、政策窗口 | 东方财富 |
| **performance-tracker** | 信号胜率统计、分级表现、反馈闭环 | 腾讯 |

## 快速开始

### 前置条件

需要 **Python 3.10 或更高版本**（macOS 默认 Python 3.9 不可用）。

```bash
# 检查 Python 版本
python3 --version   # 必须是 3.10+
```

### 安装

```bash
git clone https://github.com/Eleven1111/a-stock-agent-system.git
cd a-stock-agent-system

# 创建并激活 Python 3.10+ 虚拟环境
python3.12 -m venv .venv        # 或 python3.10 / python3.11
source .venv/bin/activate

# 安装全部依赖
python -m pip install -e ".[charts,fundamentals,research,dev]"
```

> **macOS 用户**：如果系统 `python3` 仍是 3.9，请通过
> `brew install python@3.12` 安装新版本，然后使用 `python3.12`。

### 验证

```bash
python scripts/smoke_test.py      # 6项集成检查
python -m pytest -q tests/        # 22项测试全部通过
```

### 运行

```bash
# 四维评分
python skills/stock-triage/scripts/four_dim_scorer.py 002156 通富微电 --json

# 全球市场扫描
python skills/global-market-monitor/scripts/monitor.py --summary

# 港A联动
python skills/stock-triage/scripts/hk_a_linkage.py

# 新闻→板块分析
python skills/news-to-sector/scripts/main.py "焦煤期货主力合约触及涨停"

# 持仓风控
python skills/stock-triage/scripts/portfolio_manager.py --check

# 60分钟短线入场判断
python skills/stock-triage/scripts/four_dim_scorer.py 002156 通富微电 --timeframe 60
```

### 录入持仓

```bash
python skills/stock-triage/scripts/portfolio_manager.py --add 600011 华能国际 9.10 2000
```

## 配置

```bash
# 可选：重定向运行时数据、缓存和状态（默认 ~/.hermes）
export HERMES_HOME=/path/to/hermes

# 可选：覆盖 BaoStock 备用脚本使用的 Hermes Python
export HERMES_PYTHON=/path/to/python3

# 可选：启用东方财富接口（资金流向、机构数据）
export NO_PROXY=.eastmoney.com,.gtimg.cn,.sinajs.cn

# 可选：启用新闻搜索
export SERPAPI_API_KEY=your_key
```

运行时路径统一通过 `skills/common/paths.py` 解析并支持 `HERMES_HOME`，因此可以在仓库、沙箱或 CI 中运行而不写入部署机 home。系统内置数据源健康追踪。关键数据缺失时（如 yfinance 不可用），输出 `"status": "insufficient_data"` 并拒绝给方向性判断。

## Cron 调度

所有任务定义在 [`cron/hermes-cron-manifest.json`](cron/hermes-cron-manifest.json)。调度依赖外部 [Hermes Agent](https://hermes-agent.nousresearch.com) 运行时——本仓库提供脚本，Hermes 提供时钟。

```bash
python scripts/validate_cron_manifest.py cron/hermes-cron-manifest.json
```

| 时间 | 任务 | 频率 |
|------|------|------|
| 08:15 | 全球盘前扫描 | 工作日 |
| 09:00–15:00 | 盘中异动告警 | 每5分钟 |
| 09:45, 13:45, 14:45 | 港A联动 | 工作日 |
| 10:30, 14:30 | 资金流向监控 | 工作日 |
| 15:08 | 四维批量打分 | 工作日 |
| 15:10 | 持仓风控检查 | 工作日 |
| 15:25 | 收盘Triage→Kanban派发 | 工作日 |
| 22:30 | 全球晚间扫描 | 工作日 |
| 周六 10:00 | 机构行为周报 | 每周 |
| 周日 10:00 | 胜率统计周报 | 每周 |

## 输出格式

每个打分脚本输出结构化 JSON：

```json
{
  "code": "002156",
  "name": "通富微电",
  "confidence": "high",
  "data_coverage": {"realtime": true, "kline": true, "news": true, "valuation": true},
  "weighted": 7.2,
  "grade": "A",
  "emoji": "🟢🟢",
  "advice": "推荐 — 技术面偏多，有催化支撑",
  "scores": {
    "technical": {"score": 7.5, "ma5": 58.3, "rsi6": 55.1, "detail": "MACD金叉; 价格站上MA20"},
    "sentiment": {"score": 6.0, "turnover": 4.2, "detail": "情绪中性"},
    "catalyst": {"score": 8.0, "news_count": 3, "detail": "利好催化: 封测订单饱满..."},
    "deep": {"score": 7.0, "pe": 35.2, "detail": "PE=35.2, 市值=420亿"}
  }
}
```

全球监控输出 `source_health` 并在数据不足时阻止影响评估：

```json
{
  "source_health": {
    "yfinance": {"status": "ok"},
    "serpapi": {"status": "ok"},
    "usgs": {"status": "ok"}
  },
  "impact": {
    "status": "ok",
    "alerts": [...],
    "summary": "利空：AI算力(-3), 半导体(-2)；利好：电力(+1)"
  }
}
```

关键数据缺失时：

```json
{
  "source_health": {"yfinance": {"status": "failed", "error": "yfinance not installed"}},
  "impact": {
    "status": "insufficient_data",
    "alerts": [],
    "summary": "关键市场数据不足：美股指数/VIX/美债不可用，禁止输出方向性A股判断"
  }
}
```

## 项目结构

```
a-stock-agent-system/
├── pyproject.toml              # 依赖管理
├── config/scoring.yaml         # 评分权重 & 风控参数
├── cron/hermes-cron-manifest.json  # 11个定时任务
├── scripts/
│   ├── smoke_test.py           # 6项集成验证
│   └── validate_cron_manifest.py
├── tests/                      # 12个单元测试
├── skills/
│   ├── common/                 # 共享HTTP层 + 原子状态存储
│   ├── stock-triage/           # 编排中枢
│   ├── stock-analyst/          # 技术分析引擎
│   ├── hot-money-tactics/      # 游资战法
│   ├── global-market-monitor/  # 全球宏观→A股影响
│   ├── news-to-sector/         # 产业链催化映射
│   ├── serenity-investment-research/  # 深度投研
│   ├── a-stock-commands/       # Discord快捷指令
│   ├── a-stock-data/           # 数据源参考
│   └── a-stock-daily-report/   # 每日简报模板
└── AGENTS.md                   # 项目宪法
```

## 设计原则

**故障闭合。** 关键数据缺失时输出 `insufficient_data`，绝不猜测。

**置信度先于信念。** 每项分析携带 `confidence` 字段。低置信度时阻止方向性判断。

**脚本优于服务。** 每个模块是独立的 CLI 脚本。无服务器、无数据库、无常驻进程。按需组合。

**状态原子写入。** 所有 JSON 写入通过 `state_store.atomic_write_json()`，带备份和崩溃恢复。

## 数据源

| 源 | 覆盖 | 要求 |
|----|------|------|
| 腾讯 `qt.gtimg.cn` | A股/港股实时行情 + K线 | 无 |
| Yahoo Finance `yfinance` | 美股/全球指数/VIX/期货/汇率 | `pip install yfinance` |
| 东方财富 | 资金流向、机构数据、事件日历 | `NO_PROXY=.eastmoney.com` |
| 新浪 `hq.sinajs.cn` | A股实时行情（备用） | 无 |
| SerpAPI | 全球新闻搜索 | `SERPAPI_API_KEY` |
| USGS | 全球地震监测 | 无 |
| GDACS | 飓风/洪水/火山预警 | 无 |

## 测试

```bash
pip install -e ".[dev]"
python -m pytest -q tests/        # 22项测试
python scripts/smoke_test.py      # 6项集成检查
python scripts/validate_cron_manifest.py
```

## 免责声明

本系统仅供学习研究，**不构成任何投资建议**。所有输出基于公开数据和量化规则，不保证准确性。历史表现不代表未来收益。系统不会自动下单或操作任何交易账户。

## 许可证

MIT
