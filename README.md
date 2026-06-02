# A 股全栈 Agent 系统

> 基于 Hermes Agent 的多智能体 A 股投资决策辅助系统。
> 本仓库包含 9 个 Skill 的核心脚本、数据采集引擎和分析模块。
> Cron 调度依赖外部 Hermes Agent 运行时。

## 架构

```
                      stock-triage (编排中枢)
                            │
    ┌───────┬───────┬───────┼───────┬───────┐
    ▼       ▼       ▼       ▼       ▼       ▼
 技术分析  游资情绪  全球监控  资讯催化  深度投研  (pulse-engine: 外部)
    │       │       │       │       │
    └───────┴───────┴───────┴───────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
         资金流向监控    持仓风控管理    盘中异动告警
```

## 能力矩阵

| 维度 | Skill | 覆盖 | 状态 |
|------|-------|------|------|
| 🌍 外围感知 | global-market-monitor | 美股/VIX/期货/汇率/自然灾害 | ✅ 可运行 |
| 🇭🇰 港A联动 | hk_a_linkage | AH溢价/港股异动/指数背离 | ✅ 可运行 |
| 📊 技术分析 | stock-analyst | 日/周/60/30分钟多框架 | ✅ 可运行 |
| 🔥 游资情绪 | hot-money-tactics | 涨停板/连板/情绪周期 | ✅ 需 akshare |
| 📡 资讯催化 | news-to-sector | 18条产业链映射 | ✅ 可运行 |
| 💰 资金流向 | capital_flow_monitor | 北向/主力/板块资金 | ⚠️ 需 NO_PROXY |
| 🎓 深度投研 | serenity-investment-research | 供应链/财务/估值 | ✅ 可运行 |
| 🛡️ 持仓风控 | portfolio_manager | 浮盈浮亏/止损/仓位 | ✅ 可运行 |
| ⚡ 盘中异动 | intraday_monitor | 5分钟涨跌停/放量告警 | ✅ 可运行 |
| 🏛️ 机构行为 | institution_tracker | 调研/研报/增减持 | ⚠️ 需 NO_PROXY |
| 📅 事件日历 | event_calendar | 解禁/分红/政策窗口 | ⚠️ 需 NO_PROXY |
| 📈 胜率统计 | performance_tracker | 信号命中率反馈 | ✅ 可运行 |

## 快速开始

### 安装

```bash
git clone https://github.com/Eleven1111/a-stock-agent-system.git
cd a-stock-agent-system

# 核心依赖（所有脚本必需）
python -m pip install -e ".[charts,fundamentals,research,dev]"

# 验证依赖
python -c "import yfinance, akshare, pandas, numpy; print('deps ok')"
```

### 配置

```bash
# ~/.hermes/.env（Hermes Agent 环境，非本仓库）
SERPAPI_API_KEY=your_key          # 可选：新闻搜索
NO_PROXY=.eastmoney.com,.gtimg.cn,.sinajs.cn  # 可选：资金流/机构数据
```

### 验证安装

```bash
python -m compileall -q .
python scripts/smoke_test.py
```

### 手动运行示例

```bash
# 四维评分
python skills/stock-triage/scripts/four_dim_scorer.py 002156 通富微电 --json

# 全球市场扫描
python skills/global-market-monitor/scripts/monitor.py --summary

# 港A联动
python skills/stock-triage/scripts/hk_a_linkage.py --json

# 新闻→板块分析
python skills/news-to-sector/scripts/main.py "焦煤期货主力合约触及涨停，涨幅8%"

# 持仓风控
python skills/stock-triage/scripts/portfolio_manager.py --check

# 60分钟短线入场判断
python skills/stock-triage/scripts/four_dim_scorer.py 002156 通富微电 --timeframe 60
```

## Cron 调度

**Cron 任务依赖外部 Hermes Agent 运行时。** 本仓库提供可导入的 manifest 和验证工具，不包含 Hermes 的 cron 执行环境。

```bash
# 查看所有 Cron 定义
cat cron/hermes-cron-manifest.json

# 验证 manifest
python scripts/validate_cron_manifest.py cron/hermes-cron-manifest.json
```

| 来源 | 说明 |
|------|------|
| ✅ 本仓可执行 | 所有脚本可通过命令行独立运行 |
| ⚠️ 外部 Hermes | Cron 调度、Kanban 派发、飞书推送需 Hermes Agent |
| ⚠️ 外部数据 | BuilderPulse、PulseEngine 为社会情绪项目，不在本仓 |

## 数据源

| 源 | 覆盖 | 状态 |
|----|------|------|
| 腾讯财经 `qt.gtimg.cn` | A股/港股实时行情+K线 | ✅ 免费 |
| Yahoo Finance `yfinance` | 美股/全球指数/VIX | ✅ 免费（有延迟） |
| 东方财富 `eastmoney.com` | 资金流向/机构数据 | ⚠️ 需 NO_PROXY |
| 新浪财经 `hq.sinajs.cn` | A股实时行情 | ✅ 免费 |
| SerpAPI | 全球新闻搜索 | ⚠️ 需 API key |
| USGS `earthquake.usgs.gov` | 地震监测 | ✅ 免费 |

数据源失败时系统会标记 data_coverage 和 source_health，关键数据缺失时**不输出方向性投资判断**。

## 项目结构

```
a-stock-agent-system/
├── README.md
├── AGENTS.md
├── pyproject.toml
├── skills/
│   ├── common/                🆕 共享模块（HTTP/状态存储）
│   ├── stock-triage/          🧠 编排中枢
│   ├── stock-analyst/         📊 技术分析引擎
│   ├── hot-money-tactics/     🔥 游资战法
│   ├── global-market-monitor/ 🌍 全球市场监控
│   ├── news-to-sector/        📡 资讯→板块映射
│   ├── serenity-investment-research/ 🎓 深度投研
│   ├── a-stock-data/          📦 A股数据参考
│   ├── a-stock-daily-report/  📋 每日简报模板
│   └── a-stock-commands/      ⌨️ 快捷指令
├── cron/                      🆕 Cron manifest
├── config/                    🆕 评分配置
├── tests/                     🆕 测试
└── scripts/                   🆕 smoke test + 工具
```

## 测试

```bash
python -m pytest -q
python scripts/smoke_test.py
```

## 免责声明

本系统仅供学习参考，**不构成任何投资建议**。所有分析结果基于公开数据和量化规则，不保证准确性。股市有风险，投资需谨慎。本系统不会自动下单或操作任何交易账户。
