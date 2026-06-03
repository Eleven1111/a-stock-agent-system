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

## 与 chanlun-backtest 的关系

`chanlun-backtest` 是离线研究闸门，验证某套打板或缠论规则是否有样本外统计优势；`daban-stock-picker` 是日常候选池适配器。没有通过研究闸门的参数，不应在这里被包装成“已验证有效”的实时策略。
