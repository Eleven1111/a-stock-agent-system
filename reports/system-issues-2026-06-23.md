# A股智能投研系统 — 问题诊断报告

> 日期：2026-06-23
> 基于盘中实跑数据 + 配置审计发现

---

## 问题一：候选池覆盖严重不足

### 现状
```
全市场 5207
  → 3634 合格 (排除ST/低价/停牌)
    → 350 预选 (prefilter_limit)
      → 200 候选池 (watch_limit)    ← 🔴 砍掉94%的股票
        → 20 竞价短名单 (auction_shortlist_limit)
          → 5 开盘信号 (open_confirmation_limit)
```

### 配置文件
`config/candidate_selection.json`:
- `"prefilter_limit": 350`
- `"watch_limit": 200`
- `"auction_shortlist_limit": 20`
- `"open_confirmation_limit": 5`

### 后果
- auction-quote-input 只拉了200只股票的集合竞价数据
- 数千只股票的开盘异动完全未被扫描
- 今日竞价阶段 **冰轮环境 ±9.91%** 在池内但未推送，更多不在池内的涨停票直接未知

### 建议修复
- 增大 `prefilter_limit` 到 1000-2000
- 增大 `watch_limit` 到 500-1000
- 或者删掉硬上限，改用动态评分阈值

---

## 问题二：打板/热钱管道被永久禁用

### 现状
`config/candidate_selection.json`:
```json
"hot_money_selection": {
    "research_only": true,
    "min_quote_count": 500,
    ...
}
```

- `research_only: true` → 管道只生成研究数据，**从不输出可执行信号**
- `min_quote_count: 500` → 但 watch_limit 只有 200，**永远满足不了**，所以 hot_money 管道永久冻死在 `status: "insufficient_data"`

### 今日实例
- **中钨高新 (000657)** → daban_score=100（打板评分第3名）
- **京基智农 (000048)** → daban_score=100（打板评分第1名）
- **冰轮环境 (000811)** → 竞价涨幅+9.91%
- 但 hot_money 管道状态：`"status": "insufficient_data", "daban_ready": false`
- 这些高评分情报产生了，但没有进入可执行链路，也**没有推送给用户**

### 建议修复
- 将 `research_only` 改为 `false`
- 将 `min_quote_count` 调低到 200 或更少（匹配候选池大小）
- 或改为动态：从实际候选池数量推断

---

## 问题三：多套评分系统互不通信

### 现状
系统有三套评分机制，各自独立运行，没有汇聚点：

| 管道 | 评分体系 | 筛选产出 | 是否推送 |
|------|---------|---------|:-------:|
| candidate-preopen | daban_score / trend_score | 200候选池 | ❌ |
| auction-finalize | auction_score | 5待选 | ❌ |
| open-confirmation | open_score + quality gate | 5信号(watch) | ✅ |

### 今日实例
- candidate-preopen 给 **中钨高新** 打了 daban_score=100（满分），但这信息没有传递到后续管道的最终报告
- open-confirmation 用的 trend_pullback 策略看不上中钨高新，直接丢了
- **用户永远不知道"有个股票打板评分100"**

### 建议修复
- 建立**中间情报汇聚层**，汇总所有管道的评分 TOP N
- 每阶段评分独立产出摘要，合并推送
- 即使最终不买入，"哪些票评分高但被过滤了"也是有效信息

---

## 问题四：新闻源和数据源存在断点

### 今日运行状态

| 数据源 | 状态 | 原因 |
|-------|:---:|------|
| serper 新闻API | ❌ 挂了 | `_next_serper_key` 运行时导入报错 — key 已配，但 cron 执行时 PYTHONPATH 没找到 `data_provider` 模块 |
| news-monitor-intraday | ❌ insufficient_data | 上游新闻源调用失败（serper 导入问题的连锁反应） |
| catalyst-trigger | ⚠️ no_new | 扫描0条，可能是上游数据问题 |
| provider-health | ⚠️ degraded | 整体健康度下降 |
| yfinance 美股数据 | ✅ ok | 正常 |
| GDACS 灾害数据 | ✅ ok | 正常 |

### 后果
- 盘中新闻监控几乎无数据
- 热点催化剂检测无法运行
- 全球新闻情绪面缺失

### 建议修复
- 🔴 修复 `data_provider` 模块在 cron 运行时的 PYTHONPATH 导入路径（key 已配，是路径问题）
- 增加替代新闻源（如百度新闻、新浪热点）
- fallback 到免费新闻源

---

## 问题五：情报不推送到用户

### 现状
系统产生了大量中间层数据（candidate-preopen的200只评分、auction-quote-input的竞价数据），但：
- 这些数据只存为 JSON 快照文件
- 没有生成可读摘要推送到聊天
- 辅助分析管道（social-attention、hk-a-linkage、ledger-projector）产出也仅存文件

### 建议修复
- 增加 **"早盘情报简报"** 独立推送 (8:15-9:00)
  - 候选池 TOP 10 打板评分
  - 候选池 TOP 10 趋势评分
  - 竞价阶段异动个股
- 增加 **"集合竞价简报"** 独立推送 (9:25)
  - 竞价涨幅/跌幅 TOP 5
  - daban_score ≥ 90 的股票
- 增加 **"开盘摘要"** 推送 (9:35)
  - open-confirmation 信号（含被过滤的高分票）
  - 全局市场概况

---

## 问题六：仓位管理和资金规模脱节

### 配置文件
`config/data_access.json`:
```json
"risk": {
    "portfolio_size": 100000
}
```

### 现状
- 配置的 `portfolio_size` 是 **10万元**
- 实际资金只有 **2万元**
- 导致仓位百分比计算、风控阈值与实际账户脱节

### 建议修复
- 使 `portfolio_size` 可动态配置
- 或从账户实际余额自动读取

---

## 总结：修复优先级

| 优先级 | 问题 | 影响 |
|:-----:|------|------|
| 🅿️0 | **候选池过小** (watch_limit=200) | 漏掉94%的股票，最核心瓶颈 |
| 🅿️0 | **hot_money 永久禁用** (research_only) | daban_score 满分票无法进入可执行链路 |
| 🅿️1 | **多评分系统不通信** | 中间层情报产生但不推送 |
| 🅿️1 | **新闻源故障** (serper key) | 盘中新闻监控几乎为空 |
| 🅿️2 | **无情报推送机制** | 用户只能看到最终5个信号，看不到中间发现 |
| 🅿️3 | **portfolio_size 与实际脱节** | 仓控计算偏差 |

---

*报告人：Luna | 数据来源：系统2026-06-23盘前/盘中快照 & 配置文件审计*
