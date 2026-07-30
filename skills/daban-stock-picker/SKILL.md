---
name: daban-stock-picker
description: >-
  A-share 10% limit-up candidate adapter for first-board reseal and
  second-board strengthening patterns. It applies auction evidence,
  tradeability, veto, portfolio, and T+1 controls without placing orders.
version: 1.1.0
author: Luna
metadata:
  hermes:
    tags: [A股, 打板, 游资, 集合竞价, 风控]
    category: finance
---

# 主板打板候选适配器

本技能把涨停梯队、集合竞价和开盘报价转换为可审计候选。它不替代
`hot-money-tactics` 的情绪判断，也不绕过 `stock-triage` 的组合风险 Policy。

## 运行顺序

```text
动态有效订阅扫描
  -> 前一交易日涨停与连板池
  -> 全市场竞价异常发现
  -> 可成交性检查
  -> 模式与六问否决
  -> 组合风险和 T+1 计划
  -> Signal Ledger
```

第一层必须从 `runtime_targets.py` 读取当前有效订阅，并尊重手动取消墓碑。文档和 cron
命令不得保存固定代码列表。订阅异常只获得优先检查权，不获得降低门槛的权利。

## 竞价采集

`scripts/auction_collector.py` 在集合竞价阶段累积腾讯盘口快照，并在收口时生成：

- `auction_gap_pct`
- `auction_max_gap_pct`、`auction_price_decay_pct`、`auction_faded_from_limit_up`
- `auction_bid_ask_ratio`
- `auction_net_bid_delta`
- `auction_volume`、`auction_amount`
- `board_status`（含 `limit_down`）、`limit_up`、`limit_down`
- `seal_amount_ratio_pct`

竞价短名单对跌停（`is_limit_down`）和指示价自高点回落达阈值的标的一票否决，
平开/低开与小幅回落转为可审计扣分（`auction_weakness_notes`）。

这些字段是证据，不是已验证 edge。新增阈值或权重必须先通过
`chanlun-backtest/scripts/research_gate.py` 的样本外验证。

## 过滤顺序

1. 股票池：主板 10% 涨跌幅制度、非 ST、上市时长、流通市值、流动性和价格范围。
2. 数据质量：交易日、报价、昨收、盘口和快照批次一致。
3. 市场闸门：情绪温度、昨日涨停指数、炸板率和退潮信号。
4. 持仓闸门：T+1 待处置、现金、交易次数、亏损线和集中度。
5. 模式过滤：首板回封或二板弱转强。
6. 可成交性：停牌、无报价和封死一字板一票否决。
7. 六问否决：达到配置中的否决数量时停止开仓。

阈值的单一事实源是 `config/daban_thresholds.yaml`，不得在 Skill 文档中复制一套可漂移参数。

## 竞价和开盘任务

- 调度来源：`cron/hermes-cron-manifest.json`。
- 入口：`scripts/run_agent_dag.py <job-id> --emit-target`。
- 集合竞价只读取本批次快照，开盘确认只读取已通过依赖门禁的竞价 artifact。
- 重点对象和最终候选自动写入 monitor registry；卖出、淘汰或手动取消后自动退出。
- 无法成交的高分候选必须标记阻断，不得包装成买入建议。

## 命令

```bash
PYTHONPATH=skills/common \
python skills/daban-stock-picker/scripts/auction_collector.py \
  --codes <market-prefixed-codes> --snapshot

PYTHONPATH=skills/common \
python skills/daban-stock-picker/scripts/auction_collector.py \
  --codes <market-prefixed-codes> --finalize --json

PYTHONPATH=skills/common \
python skills/daban-stock-picker/scripts/daban_candidate_api.py \
  --input <candidate-file> --json
```

生产调度不得在 `--codes` 中写固定观察列表；代码集合由运行时目标和本批次候选 artifact 生成。

## 输出

- `blocked`
- `top_candidates`
- `block_reasons`
- `tradeability`
- `six_question_veto`
- `entry_plan`
- `t1_exit_plan`
- `record_payload`
- 数据质量、来源时间、`batch_id` 和 Ledger 关联标识

## T+1

当日新买入股份不能当日止损卖出。每个候选必须在入场前生成次一交易日处置方案，包括竞价
弱于预期、开盘承接失败、隔夜跳空和无法成交场景。交易日历未覆盖时阻断计划生成。

## 验证

```bash
pytest -q tests/test_auction_collector.py tests/test_open_confirmation.py
python scripts/validate_cron_manifest.py
git diff --check
```
