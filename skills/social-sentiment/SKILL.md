# A股社会关注度

## 定位

采集股票在东方财富、雪球及可选百度热搜中的关注度，输出可复现的
`social_attention_snapshot_v1`。它是候选发现和情绪评分的弱证据，不是独立买入信号。

## 运行

```bash
python skills/social-sentiment/scripts/collect.py --json
```

运行结果写入：

- 不可变快照：`$A_STOCK_STATE_HOME/market/snapshots/{date}/social-attention/`
- 最近可信缓存：`stock-triage/cache/social_attention.json`
- 共享信号上下文：`stock-triage/cache/signal_context.json`

## 约束

- 至少两个独立平台同时覆盖某只股票，才允许影响排名或评分。
- 单一来源只能展示，不能加分。
- 候选发现调整范围不超过 `±3` 分。
- 四维情绪面调整范围不超过 `±0.8` 分。
- 高关注但价格走弱时按拥挤背离处理。
- 任一来源失败不得阻断候选发现、竞价、开盘确认或四维评分。
- 公告、可成交性、T+1 和组合风险门禁始终优先。
