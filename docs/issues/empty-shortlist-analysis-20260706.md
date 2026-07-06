# [Bug] 执行短名单连续空仓：lianban_ladder 数据缺失导致全链路断链

## 摘要

2026-07-03（周五）和 2026-07-06（周一）A 股打板选股系统的 execution shortlist 连续为空。当前证据指向同一个主因：`hot-money-context` 没有持续写入可用的 `lianban_ladder`（连板梯队）上下文，导致游资/龙头择时状态进入 `insufficient_data`，之后被 delivery gate 放大为所有候选不可交付。

本问题需要优先修复数据链路和交付门禁语义，否则即使候选池、行情报价、竞价因子正常，最终仍会出现“系统状态 ready，但执行名单为空”的假安全状态。

## 影响

- 2026-07-03 和 2026-07-06 两个交易日 execution shortlist 连续空仓。
- 2026-07-06 的 `auction_shortlist_2026-07-06.json` 中：
  - `shortlist_count=0`
  - `decision_count=0`
  - `input_count=406`
  - `rejected_count=656`
  - 所有 rejected 记录的 `rejection_reasons` 都是 `["候选质量门槛 qualified=False"]`
- 2026-07-06 候选全部落在 `trend_pullback`，但仍被 `leader.qualified=false` 这一游资/龙头语义拦截。

## 核心链路

```text
lianban_ladder 缺失
  -> market_timing.status=insufficient_data
  -> market_timing.daban_ready=false
  -> 所有候选 hot_money_qualified=false
  -> selection_context.leader.qualified=false
  -> auction delivery gate 判定 qualified=False
  -> execution shortlist 全灭
```

## 关键证据

### 1. hot-money-context 从 2026-07-03 开始没有可用结果

`cron/output/hot-money-context/` 中可见历史正常结果到 2026-06-23，2026-07-03 只留下 lock 文件：

- `cron/output/hot-money-context/hot-money-context-20260703-151800-49808.json.lock`

2026-07-05 的 wrapper artifact 存在，但状态是 `skipped_non_trading_day`，没有输出新的有效 `signal_context`：

- `cron/output/hot-money-context/hot-money-context-20260705-100000-72918.json`

实际 cron 入口不是 `skills/daban-stock-picker/scripts/hot_money_context.py`。当前路径应以这些文件为准：

- `cron/hermes-cron-manifest.json`：`hot-money-context` job，命令是 `python scripts/run_agent_dag.py hot-money-context --emit-target`
- `scripts/run_agent_dag.py`：DAG 入口
- `skills/stock-triage/scripts/candidate_discovery.py`：下游读取 `signal_context` 并构建 `hot_money_selection`
- `skills/common/signal_context.py`：`lianban_ladder` 上下文读写

### 2. lianban_ladder 缺失被 market_temperature 明确识别

`skills/common/market_temperature.py` 中：

- `compute_temperature()` 在 `ladder` 为空时返回 neutral，并写入 `notes=["lianban_ladder 缺失"]`
- `temperature_from_context()` 在没有 `lianban_ladder` 时返回 `_neutral("lianban_ladder 缺失", ...)`

对应代码位置：

- `skills/common/market_temperature.py:174`
- `skills/common/market_temperature.py:224`

### 3. hot_money_selection 把缺梯队升级为 insufficient_data

`skills/common/hot_money_selection.py` 的 `build_market_timing()`：

- 调用 `temperature_from_context(..., max_age_days=0)`
- 当 `temperature.context_fresh` 为 false 时，把 temperature notes 加入 reasons
- `ready = not reasons`
- `daban_ready = ready`
- `previous_ladder_premium = None`（没有上一交易日梯队映射可计算）

对应代码位置：

- `skills/common/hot_money_selection.py:115`
- `skills/common/hot_money_selection.py:151`
- `skills/common/hot_money_selection.py:165`
- `skills/common/hot_money_selection.py:179`

2026-07-06 的候选池证据：

```json
{
  "hot_money_selection": {
    "status": "insufficient_data",
    "daban_ready": false,
    "market_timing": {
      "status": "insufficient_data",
      "daban_ready": false,
      "previous_ladder_premium": null,
      "tier": "neutral"
    },
    "reasons": ["市场择时证据未通过"]
  }
}
```

### 4. hot_money_qualified 全部变 false

`skills/common/hot_money_selection.py` 中 `apply_leader_identity()` 把 `hot_money_qualified` 定义为：

```text
state.daban_ready
and sector_state.qualified_for_daban
and rank <= leader_top_n
and daban_eligible
```

当 `daban_ready=false` 时，所有候选都会被写成 `hot_money_qualified=false`，并带上 `hot_money_gate_reasons=["游资选股状态不可用", ...]`。

对应代码位置：

- `skills/common/hot_money_selection.py:465`
- `skills/common/hot_money_selection.py:471`

### 5. auction delivery gate 把 leader.qualified 当成通用质量门槛

`selection_context_for()` 和 `advance_selection_context()` 会把 `hot_money_qualified` 写入 `leader.qualified`：

- `skills/common/hot_money_selection.py:562`
- `skills/common/hot_money_selection.py:603`

`assess_delivery_quality()` 又从 `leader_context.qualified` 取出 `qualified`，只要是 `False` 就直接 reject：

- `skills/common/weak_market_delivery.py:174`
- `skills/common/weak_market_delivery.py:246`

这会造成语义混用：`leader.qualified=false` 原本是游资/主线/龙头资格，不应该无条件等同于所有 lane 的通用候选质量门槛。2026-07-06 的 `trend_pullback` 候选也被这个门槛全部拦掉。

### 6. auction finalize 本身只是传播了空名单

`skills/daban-stock-picker/scripts/auction_collector.py` 的 `finalize()` 调用：

- `candidate_pipeline.rank_auction_shortlist(...)`
- 然后把 `result["shortlist"][:5]` 作为 `top_candidates`
- `decision_count=len(decisions)`

2026-07-06 结果：

```json
{
  "schema": "auction_finalize_v2",
  "asof": "2026-07-06",
  "status": "ready",
  "shortlist_count": 0,
  "decision_count": 0,
  "top_candidates": []
}
```

对应代码位置：

- `skills/daban-stock-picker/scripts/auction_collector.py:236`
- `skills/daban-stock-picker/scripts/auction_collector.py:241`
- `skills/daban-stock-picker/scripts/auction_collector.py:255`
- `skills/daban-stock-picker/scripts/auction_collector.py:390`

### 7. 对比 2026-07-02：hot-money-context 正常时有 3 个短名单

`skills/daban-stock-picker/data/auction_shortlist_2026-07-02.json`：

- `shortlist_count=3`
- top 3 都是 `daban:mainline_leader_confirm`
- `hot_money_qualified=true`
- `market_timing.status=ready`
- `daban_ready=true`
- `previous_ladder_premium=3.0819`

这说明 shortlist 机制本身在 hot-money context 正常时可以产出结果。

### 8. 行情候选与报价不是主因

2026-07-06 的 candidate-preopen 仍能产出 ready 候选池和 top candidates。问题发生在 hot-money selection 和后续 delivery gate，而不是行情完全不可用。

需要注意：本地观察到的 2026-07-06 wrapper artifact 一度出现 `quote_count=5204` 的 market snapshot 证据，但当前 main 工作树存在大量未提交/生成文件波动，建议 fable5 在修复时重新读取 `market/snapshots/2026-07-06/` 或生产 artifact 做最终确认。

### 9. chanlun 不是空名单主因

`auction_shortlist_2026-07-06.json` 中 chanlun 状态统计：

- `no_signal`: 506
- `display_only`: 150
- `allowed_in_live_agent=false`

chanlun 当前表现为保护性研究闸门，不是导致 `shortlist_count=0` 的直接原因。

## 附带问题：lane 语义混用

当前代码已经有一部分 lane 区分：

- `candidate_pipeline.rank_auction_shortlist()` 中 `_lane_member()` 只在 `lane == "daban"` 时检查 `hot_money_qualified`
- `auction_collector.finalize()` 中也会把 strategy 分成 `daban` / `trend`

但 `weak_market_delivery.assess_delivery_quality()` 在更底层直接把 `qualified=False` 作为通用 reject 条件，没有检查 lane：

```python
if qualified is False:
    status = "reject"
    reasons.append("候选质量门槛 qualified=False")
```

这会使 trend lane 也继承游资/龙头资格失败，最终所有 trend 候选被拒绝。2026-07-06 的 rejected 记录全部是 `trend_pullback`，但拒绝理由仍是 `候选质量门槛 qualified=False`。

## 建议修复短名单

| 优先级 | 操作 | 说明 |
| --- | --- | --- |
| P0 | 修复 `hot-money-context` 数据链路 | 排查 2026-07-03 为什么只留下 `.lock` 没有 `.json` 结果；`run_agent_dag.py hot-money-context` 应在缺结果时显式失败或告警 |
| P0 | 给 DAG 加硬检查 | 下游 `candidate-discovery` / `candidate-preopen` 在 `signal_context.lianban_ladder` 缺失时不应默默产出看似 ready 的 execution 输入 |
| P1 | 修 auction delivery gate 语义 | `daban` lane 使用 `hot_money_qualified` / `leader.qualified`；`trend` lane 应使用趋势自己的质量门槛，不应被游资主线资格硬拦 |
| P1 | 缺梯队数据时降级为 degraded | 不要让系统整体显示 ready 后在 execution 层全灭；建议输出 `degraded_missing_lianban_ladder` 并保留 trend research/watch 交付路径 |
| P2 | chanlun 暂不改 | 当前 chanlun 保护机制正常，不是空名单直接原因 |

## 建议验收标准

1. `hot-money-context` 在交易日必须产出 `.json` 结果；只有 `.lock` 时 cron/DAG 状态应为 failed 或 degraded，且可被监控发现。
2. `signal_context` 缺少 `lianban_ladder` 时，`market_timing.reasons` 必须包含具体原因，并被上游 artifact 显式展示。
3. `trend_pullback` 候选不再因为 `leader.qualified=false` 被 `weak_market_delivery` 无条件 reject。
4. 新增回归测试覆盖：
   - missing `lianban_ladder` -> `hot_money_selection.status=insufficient_data`
   - `lane="daban"` 且 `hot_money_qualified=false` -> reject
   - `lane="trend"` 且 only `hot_money_qualified=false` -> 不因 `候选质量门槛 qualified=False` 被 reject
   - 只有 `.lock` 无结果的 upstream dependency -> 下游 DAG 不应标记为正常 ready

## 相关文件

- `cron/hermes-cron-manifest.json`
- `scripts/run_agent_dag.py`
- `skills/common/signal_context.py`
- `skills/common/market_temperature.py`
- `skills/common/hot_money_selection.py`
- `skills/common/weak_market_delivery.py`
- `skills/common/candidate_pipeline.py`
- `skills/stock-triage/scripts/candidate_discovery.py`
- `skills/daban-stock-picker/scripts/auction_collector.py`
- `skills/daban-stock-picker/scripts/daban_candidate_api.py`
- `skills/daban-stock-picker/data/auction_shortlist_2026-07-06.json`
- `skills/daban-stock-picker/data/auction_shortlist_2026-07-02.json`
- `config/daban_thresholds.yaml`
- `config/candidate_selection.json`
- `skills/chanlun-backtest/`
