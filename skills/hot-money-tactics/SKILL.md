---
name: hot-money-tactics
description: 游资战法综合分析。每日涨停板全景、连板梯队、封板质量、板块热点、市场情绪、游资情绪周期判断。
  支持查询指定日期，自动生成完整的游资数据分析报告。适用于打板选手、短线交易者观察市场情绪和热点方向。
version: 1.0.0
author: Hermes Agent
metadata:
  hermes:
    tags: [A股, 股票, 游资, 涨停板, 连板, 短线, 打板]
    category: finance
---

# 游资战法 — 涨停板全景分析

基于 AkShare 提供完整的游资数据分析能力，覆盖：涨停板连板梯队、封板质量、板块热度、大盘情绪、游资情绪周期判断。

## 触发方式

用户提到以下关键词时自动触发：
- "游资"、"游资战法"、"打板"、"涨停"、"连板"
- "今天哪些涨停"、"涨停板分析"、"短线情绪"
- "情绪周期"、"今天能打板吗"、"明天买什么"
- "异动上榜"、"市场情绪"、"涨停复盘"
- "板块轮动"、"今天什么板块热"、"板块切换"（触发 --rotation）
- "封测"、"半导体涨停"、"哪个板块涨停最多"（触发板块热度分析）
- "连板高度"、"最高板"、"龙头是谁"（触发连板梯队分析）

也可手动加载：`/skill hot-money-tactics`

## 使用

```bash
# 今日分析（必须用 Hermes venv 的 Python，因为 AkShare 装在里面）
~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/skills/hot-money-tactics/scripts/analyze.py

# 指定日期
~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/skills/hot-money-tactics/scripts/analyze.py 20260601

# 完整报告模式（含大盘情绪）
~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/skills/hot-money-tactics/scripts/analyze.py 20260601 --all

# 板块轮动追踪（对比最近5个交易日）
~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/skills/hot-money-tactics/scripts/analyze.py --rotation
```

## 分析模块 ⭐

数据源说明见 `references/data_sources.md`。

### 1️⃣ 连板梯队
按连板数从高到低排列，标记每只股票的：
- 封单资金（亿）
- 是否炸板
- ⭐ 优质板标记（早盘秒封 + 封单>1亿）

### 2️⃣ 封板质量
- 集合竞价封板数/比例
- 早盘封板数/比例
- 午盘后封板数/比例
- 炸板数/炸板率
- 封单资金TOP10

### 3️⃣ 板块热度
按板块涨停家数排名，展示：
- 涨停家数
- 最高连板
- 具体个股列表

### 4️⃣ 大盘情绪
- 涨跌家数、涨跌比
- 涨停/跌停数量
- 全A成交额

### 5️⃣ 情绪周期判断 ⭐
多维评分体系自动判断当前游资情绪处于哪个阶段：
- 🔥🔥🔥 沸点区 — 情绪亢奋，分歧随时来临
- 🔥 回暖区 — 情绪好转，可参与
- 🌤 震荡区 — 情绪中性
- 🌧 冰点区 — 情绪低迷，多看少动

判断依据：涨停数 + 连板高度 + 封板率 + 竞价封板比例

### 6️⃣ 板块轮动追踪 ⭐（`--rotation`）
对比最近 N 个交易日，自动识别：
- 🔥 **持续热点** — 连续多天热度的板块
- 🌱 **新冒头板块** — 今日才爆发的方向
- ❄️ **退潮板块** — 热度消退的方向
- 🧭 **轮动方向** — 资金在板块间的流动方向

每日展示板块 TOP5 涨停家数，一目了然看清轮动节奏。

## 已知限制

- 数据全量依赖 AkShare（东方财富数据源）
- 非交易日无涨停板数据
- 龙虎榜席位数据需单独扩展（AkShare部分接口受限）
- 盘中数据只在交易时段有效，收盘后最为准确
- 本机 ClashX TUN 模式下 `push2.eastmoney.com` 不可达，个股行情走腾讯 API 降级（详见 `references/data_sources.md`）
- 历史K线数据：腾讯 ifzq（`-sL` 跟随重定向）可用，BaoStock（免费，无需key）可用
- ⚠️ **ClashX TUN 模式**会阻断开往 `push2.eastmoney.com` 的流量（DNS 被解析为假 IP `198.18.x.x`），但以下 AkShare 接口已验证可绕过：
  - ✅ `stock_zt_pool_em()` — 涨停板池（核心数据）
  - ✅ `stock_zt_pool_strong_em()` — 强势股池
  - ❌ `stock_zh_a_spot_em()` — 全A股行情（走 push2）
- 大盘情绪模块已内置 **Tencent API 降级方案**（`qt.gtimg.cn`），TUN 模式下自动回退到指数级别数据
- `--rotation` 模式依次请求近 N 个交易日数据，耗时较长（每个交易日一个API调用）

## ⚠️ 重要提醒

本工具仅供学习参考，不构成任何投资建议。股市有风险，短线交易风险极高。
