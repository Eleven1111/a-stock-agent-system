---
name: daban-stock-picker
description: >-
  A股主板10cm打板候选池。用于游资打板范式下的首板回封、二板弱转强筛选、
  六问否决、可成交性闸门和T+1处置计划。适合盘前/盘中把结构化候选数据转换为
  stock-triage 可消费的 JSON，不自动下单。
version: 1.0.0
author: Luna
metadata:
  hermes:
    tags: [A股, 打板, 游资, 涨停, 候选池, 风控]
    category: finance
---

# A股主板10cm打板候选池

这是 `stock-triage` 的打板策略适配层，不替代 `hot-money-tactics` 的情绪分析，也不绕过可成交性风控。它把盘前复盘、集合竞价和早盘盘口形成的结构化候选输入，转成可审计的候选池 JSON。

定位：
- `hot-money-tactics`：判断短线情绪、题材强度、连板梯队。
- `daban-stock-picker`：在打板范式下做机械过滤、六问否决、仓位和 T+1 处置计划。
- `stock-triage`：汇总候选、记录信号、决定是否推送人工确认。

本 skill 只输出分析和计划，不自动下单、不操作账户。

## 输入要求

输入为 JSON，包含三块：

```json
{
  "asof": "2026-06-03",
  "market": {
    "sentiment_score": 8,
    "main_theme": "半导体",
    "yday_limitup_index_open": 0.6,
    "broken_rate_first20m": 18,
    "sectors": [{"name": "半导体", "limitup_count": 5}]
  },
  "portfolio": {
    "has_positions_to_dispose": false,
    "week_trades": 1,
    "day_loss_pct": 0,
    "week_loss_pct": 0.5,
    "consecutive_losses": 0
  },
  "candidates": [
    {
      "code": "002156",
      "name": "通富微电",
      "sector": "半导体",
      "pattern": "first_board_reseal",
      "price": 11,
      "prev_close": 10,
      "open": 10.2,
      "high": 11,
      "low": 10.1,
      "volume": 500000
    }
  ]
}
```

## ⚠️ 集合竞价分析工作流（Agent 执行规范）

任何 agent 在做集合竞价分析时，**扫描顺序不可颠倒**：

```
第一步：用户跟踪标的竞价扫描（强制性，防漏核心标的）
   └─ 获取跟踪标的列表 → auction_collector --once 逐个查竞价状态
   └─ 异常信号（竞价放量/高开>3%/低开<-2%）→ 优先上报
   
第二步：昨日涨停票竞价扫描（1进2 / 2进3 判断）
   └─ 查竞价涨幅 gap_pct 是否在弱转强窗口（-1%~+3%）或强更强窗口（>+3%）
   
第三步：全市场竞价放量扫描（新首板候选）
   └─ 竞价量/昨量 > 3倍 + 竞价涨幅 > 0% → 新方向候选
   └─ 输出结构化 JSON 喂给 daban_candidate_api 做六问否决
```

**曾犯错误（2026-06-04，已修复）：**
- ❌ 集合竞价分析只聚焦"昨涨停→1进2"框架，漏了跟踪标的太极实业 600667（竞价低开-1.82%→开盘后走强→09:45涨停）
- ❌ 通富微电33亿封单涨停时绕过去推荐康强电子（跟踪标的异常巨量信号必须优先提示）
- ✅ 正确做法：跟踪标的任何竞价异动必须优先分析，即使不在预设框架内

详细教训见 `references/tracked-stocks-first-principle.md`（通富微电+太极实业两次事故复盘）。

## 过滤顺序

1. 股票池硬过滤：主板 10cm、非 ST、上市满 60 天、流通市值 15-120 亿、20 日成交额不低于 2 亿、前收盘 4-35 元。
2. 市场闸门：昨日涨停指数开盘不弱于 -2%，早盘 20 分钟炸板率不高于 35%。
3. 持仓闸门：T+1 待处置持仓优先；本周新开不超过 3 笔；触发日/周亏损线或连续亏损线则停手。
4. 模式过滤：首板回封或二板弱转强。
5. 可成交性：封死一字板、停牌、行情缺失一票否决；普通封板标记为 `risky` 但不自动否决。
6. 每日六问：6 项中 2 项或更多为否，则当天不买。

详细参数见 `references/daban_filter_spec.md`。

## 可执行脚本

```bash
PY=~/.hermes/hermes-agent/venv/bin/python3
SDIR=~/.hermes/skills/daban-stock-picker/scripts

# 示例 JSON
$PY $SDIR/daban_candidate_api.py --example --json

# 从文件读取候选池
$PY $SDIR/daban_candidate_api.py --input candidates.json --json

# 人类可读报告
$PY $SDIR/daban_candidate_api.py --input candidates.json
```

## 输出字段

- `blocked`：当前是否没有可执行候选。
- `top_candidates`：最多 3 只通过闸门的候选。
- `candidates[].block_reasons`：所有否决原因。
- `candidates[].tradeability`：涨跌停、一字板、停牌判定。
- `candidates[].six_question_veto`：六问逐项结果。
- `candidates[].entry_plan`：初始仓位、单票上限、总仓上限。
- `candidates[].t1_exit_plan`：T+1 机械处置规则。
- `candidates[].record_payload`：可传给 `performance_tracker` 的信号记录草案，包含 `strategy_id`，调用方可用 `--strategy-id` 保留打板策略归因。

## 集合竞价因子采集（auction_collector）

`scripts/auction_collector.py` 在 9:15-9:25 用腾讯五档盘口（免费、全天候、不受 TUN 影响）算出 6 个真竞价因子，把单一手填的 `auction_gap_pct` 升级为可审计输入：

- `auction_gap_pct`：真·竞价高开幅度（现价/昨收）。
- `auction_bid_ask_ratio`：五档委买和/委卖和。
- `auction_net_bid_delta`：9:20→9:25 委买净增（9:20 后不可撤单，逼近真实意图；需多次快照）。
- `board_status`：`yizi_seal`（竞价一字封死）/ `limit_up_with_ask` / `high_open` / `flat_or_low_open`。
- `seal_amount_ratio_pct`：竞价封单额/流通市值。
- `auction_volume` / `auction_amount`：竞价撮合量与金额。

```bash
# cron 9:15-9:25 每 ~10s 累积快照
$PY $SDIR/auction_collector.py --codes sh600519,sz002156 --snapshot
# 9:25 收口算因子
$PY $SDIR/auction_collector.py --codes sh600519,sz002156 --finalize --json
```

⚠️ 这些因子是**输入升级**，不是已验证的实盘阈值。把它们接入候选打分前，必须先过 `chanlun-backtest` 研究闸门验证样本外 edge；逐笔撤单率等更强信号需 L2 付费数据。

## 🧭 用户偏好：非打板推荐（3%+ 中度上涨）

用户明确偏好"不打板，找涨幅3%+的中度上涨标的"。这意味着：

- 当用户问"今天有什么推荐"时，优先筛选**涨幅3~10%且未封涨停**的标的
- 封死涨停的票即使通过六问，也需标注"涨停已封死，明日竞价再看"
- 跟踪标的中出现**放量+3%~8%涨幅**的品种，优先提示趋势机会
- 炸板回落的票（开盘涨停→回落到+5%左右）如有板块支撑，可作为博弈候选
- ⚠️ 全市场数据获取受限时（TUN模式挡push2），优先用 `stock_zt_pool_em()` 获取涨停池 + 腾讯API查个股 + 行业板块数据，缩小范围再做深度分析

---

## ⚠️ Cron 部署要求（脚本写完≠自动运行）

撰写或修改本 skill 下任何 workflow 后必须用 cronjob 工具注册对应调度，否则无人值守时不会执行。

教训：2026-06-05 竞价收口流程已完整规划+脚本 (auction_collector.py + daban_candidate_api.py) 就绪，但未设 cron job → 用户问"集合竞价数据报告呢？""09:35开盘确认结果呢？"才发现都没跑。

### 早盘流水线部署清单

| 时间 | 任务 | 依赖 skill | 说明 |
|:----|:----|-----------|:----|
| 09:25 | 集合竞价收口+候选池 | `daban-stock-picker` + `hot-money-tactics` + `stock-triage` | 跟踪标的竞价扫描 → 昨涨停1进2过滤 → 六问否决 → 候选池 |
| 09:35 | 开盘确认+上车判定 | `daban-stock-picker` + `hot-money-tactics` + `stock-analyst` | 开盘5分钟确认封板/放量/走弱 → 持仓止损检查 |

### 跟踪标的代码（带市场前缀）

```
sh600011 sh600310 sz002156 sh600584 sz002185 sz000021 sh600667
```

新增/删除标的时同步更新所有依赖 `--codes` 的 cron job prompt。

### 部署后逐项确认

1. ❓ cron job 注册了？→ `cronjob action=list` 检查
2. ❓ 调度时间正确？→ 错开整点冲突
3. ❓ 重复模式？→ `repeat=once` 跑完自动删除，需重复用 `repeat=forever`
4. ❓ skills 字段齐全？→ 包含运行所需的全部 skill

---

## 与 chanlun-backtest 的关系

`chanlun-backtest` 是离线研究闸门，验证某套打板或缠论规则是否有样本外统计优势；`daban-stock-picker` 是日常候选池适配器。没有通过研究闸门的参数，不应在这里被包装成“已验证有效”的实时策略。
