# A 股全栈 Agent 系统

> 基于 Hermes Agent 的多智能体 A 股投资决策辅助系统。
> 10 个专业 Skill × 21 个定时 Cron × 覆盖选股→持仓→风控全链路。

## 一句话

每天从 08:15 到 22:30 全自动运行，覆盖外围感知→内部扫描→四维评分→持仓风控→胜率反馈的完整投资决策闭环。

## 架构

```
                      stock-triage (编排中枢)
                            │
    ┌───────┬───────┬───────┼───────┬───────┐
    ▼       ▼       ▼       ▼       ▼       ▼
 技术分析  游资情绪  全球监控  资讯催化  深度投研  社会情绪
    │       │       │       │       │       │
    └───────┴───────┴───────┴───────┴───────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
         资金流向监控    持仓风控管理    盘中异动告警
```

## 能力矩阵

| 维度 | Skill | 覆盖 |
|------|-------|------|
| 🌍 外围感知 | global-market-monitor | 美股/VIX/期货/汇率/自然灾害/地缘 |
| 🇭🇰 港A联动 | hk_a_linkage | AH溢价/港股异动/指数背离 |
| 📊 技术分析 | stock-analyst | 日/周/60/30分钟多框架+全市场扫描 |
| 🔥 游资情绪 | hot-money-tactics | 涨停板/连板梯队/封板质量/情绪周期 |
| 📡 资讯催化 | news-to-sector | 18条产业链映射+预期差分析 |
| 💰 资金流向 | capital_flow_monitor | 北向/主力/板块资金 |
| 🎓 深度投研 | serenity | 供应链/财务拆解/估值赔率/熊市审计 |
| 🛡️ 持仓风控 | portfolio_manager | 浮盈浮亏/止损止盈/仓位集中度 |
| ⚡ 盘中异动 | intraday_monitor | 5分钟涨跌停/放量/急涨急跌告警 |
| 🏛️ 机构行为 | institution_tracker | 调研/研报/增减持 |
| 📅 事件日历 | event_calendar | 解禁/分红/政策窗口 |
| 📈 胜率统计 | performance_tracker | 信号命中率+分级表现反馈 |

## 快速开始

### 依赖
```bash
pip install yfinance curl_cffi akshare
```

### 配置
```bash
# ~/.hermes/.env
SERPAPI_API_KEY=your_key
NO_PROXY=.eastmoney.com,.gtimg.cn,.sinajs.cn
```

### 安装 Skill
将所有 `skills/` 目录下的子目录复制到 `~/.hermes/skills/`：
```bash
cp -r skills/* ~/.hermes/skills/
```

### 录入持仓（首次）
```bash
cd ~/.hermes/skills/stock-triage/scripts
python3 portfolio_manager.py --add 600011 华能国际 9.10 2000
```

### 手动运行
```bash
# 四维评分
python3 four_dim_scorer.py 002156 通富微电

# 全球市场
python3 global_market_monitor.py --summary

# 港A联动
python3 hk_a_linkage.py
```

## Cron 调度（工作日）

| 时间 | 任务 | 层级 |
|------|------|------|
| 08:15 | 全球盘前扫描 | 🟢 |
| 09:00-15:00 | 盘中异动（5分钟） | 🔴 |
| 09:45/13:45 | 港A联动 | 🟢 |
| 10:30/14:30 | 资金流向 | 🟡 |
| 15:08 | 四维打分 | 🟡 |
| 15:10 | 持仓风控 | 🔴 |
| 15:25 | 收盘Triage | 🟡 |
| 22:30 | 全球晚间扫描 | 🟢 |
| 周六 | 机构行为周报 | ⚪ |
| 周日 | 胜率统计周报 | ⚪ |

## 数据源

所有数据来自公开免费 API：

| 源 | 覆盖 |
|----|------|
| 腾讯财经 `qt.gtimg.cn` | A股/港股实时行情+历史K线 |
| 新浪财经 `hq.sinajs.cn` | A股实时行情 |
| Yahoo Finance `yfinance` | 美股/全球指数/期货/VIX |
| 东方财富 `eastmoney.com` | 资金流向/机构调研/事件日历 |
| SerpAPI | 全球新闻搜索 |
| USGS `earthquake.usgs.gov` | 全球地震监测 |

## 项目结构

```
skills/
├── stock-triage/          🧠 编排中枢 + AGENTS.md
│   ├── scripts/           ← 四维打分/港A联动/资金流/持仓/异动/机构/事件/胜率/飞书
│   ├── references/        ← 8场景定义/多Agent架构
│   └── data/              ← 运行时数据（不入库）
├── stock-analyst/         📊 技术分析引擎
├── hot-money-tactics/     🔥 游资战法
├── global-market-monitor/ 🌍 全球市场监控
├── news-to-sector/        📡 资讯→板块映射
├── serenity-investment/   🎓 Serenity深度投研
├── a-stock-data/          📦 A股数据参考
├── a-stock-daily-report/  📋 每日简报模板
└── a-stock-commands/      ⌨️ 快捷指令（/deep /scan /global）
```

## 免责声明

本系统仅供学习参考，**不构成任何投资建议**。所有分析结果基于公开数据和量化规则，不保证准确性。股市有风险，投资需谨慎。本系统不会自动下单或操作任何交易账户。
