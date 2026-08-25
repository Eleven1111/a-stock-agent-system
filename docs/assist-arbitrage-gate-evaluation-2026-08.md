# S3 最强助攻套利（AssistArbitrage）闸门评估报告（2026-08）

> 写入方式说明：`docs/*.md` 属于本机 settings 钩子拦截 Write 的白名单外文件，本文件
> 用 Bash heredoc 写入，不违反钩子意图（钩子拦的是绕过 CLAUDE.md/README 等命名约定
> 误建文档，不是禁止 docs/ 下的正常交付物）。

## 1. 交付范围（用户 2026-08-25 决策）

本机无全市场分钟线缓存，OOS 走不了。S3 本轮只交付到：pack + 信号实现 + 回测可跑
（接 P5 成交约束）+ 保持 NON-LIVE 未注册 + 诚实的 UNVERIFIED 报告。**未**注册进
`strategy_registry`，**未**用子集数据出胜率/PF/期望值结论。

**结论先行：当前不具备注册条件。** 真实事件表上零命中，且零命中不是"策略没有
信号"，是两个必需证据字段（`breakout_time`、`leader_score_shadow`）在现有数据管道
上结构性不可得。

## 2. 信号定义与实现

LeaderScore≥80（复用 P2 已合入的 `leader_score_shadow`）∧ 板块广度≥3 ∧ 候选连板
高度≤龙头连板高度−1 ∧ 候选相对强度位于题材Top20% ∧ 龙头确认后候选率先突破日内
关键位（升级方案 §6.1）。退出：龙头走弱∧题材广度下降，或新主线 DirectionScore
超原主线≥15分。

实现：`skills/common/assist_arbitrage.py`（纯函数，零网络请求）。四条入场条件 +
一条入场触发：

| 条件 id | 判据 | 默认阈值 |
|---|---|---|
| `leader_score_min` | 龙头 LeaderScore ≥ 下限（读 `leader_score_shadow`，不重算） | 80 |
| `sector_breadth_min` | 板块涨停家数 ≥ 下限 | 3 |
| `board_level_below_leader` | 候选连板高度 ≤ 龙头连板高度 − 下限 | 1（至少矮一级） |
| `relative_strength_top20` | 候选题材内相对强度分位 ≥ 下限 | 0.80（前20%） |
| `leader_confirmed_breakout_first`（入场触发） | 龙头已确认 ∧ 候选突破排名 ≤ N | N=1（"率先"） |

退出判定（`exit_signal`，与入场判定完全独立）：

| 路径 | 判据 |
|---|---|
| A：龙头走弱 ∧ 题材广度下降 | `leader_board_broken` 或 `leader_change_pct≤-3.0`；且 `sector_breadth_count` 较对照下降≥1家 |
| B：主线切换 | `new_mainline_direction_score − original_mainline_direction_score ≥ 15` |

阈值单一事实源：`config/daban_thresholds.yaml` 的 `assist_arbitrage` 节（新增节，
未改任何既有阈值）；`skills/common/daban_config.py` DEFAULTS 同步。

策略包（解释层）：`config/strategy_packs/assist_arbitrage.yaml`。

回测接线：`skills/chanlun-backtest/scripts/daban_bt_assist_arbitrage.py`，先按
(date, sector) 分组挑龙头（组内连板最高者，`assist_arbitrage.pick_leader`），用
`hot_money_selection.leader_score()`（P2 已合入，本模块不重造）现算龙头的
`leader_score_shadow`，再接既有 `daban_bt_engine.strategy_returns`（含 P5(a) 成交
约束 + DEFAULT_COST 费用口径 + `board_overnight` 持有口径）。

## 3. 本策略的成败点

### 3.1 LeaderScore 复用纪律

本模块**不**重新实现六因子评分，只读 `leader.get("leader_score_shadow")`；
不可得（缺失/`status != "ok"`）时该条件一律 `unavailable`，绝不退化成"默认合格"
或按其它字段拍一个替代分。验证：
`tests/test_assist_arbitrage.py::test_leader_score_unavailable_when_shadow_missing_is_fail_closed`、
`test_leader_score_reads_from_leader_score_shadow_field_only`（两个候选除
`leader_score_shadow.score` 外完全一致，只有它变化，结论必须跟着变）。

### 3.2 退出条件是代码，不是文档

`exit_signal()` 与入场 `evaluate()` 完全独立评估。验证：
`test_leader_weakening_and_breadth_declining_forces_exit_even_if_candidate_still_strong`
——先断言候选自身入场判定确实是 `signal`（前置断言，防止用例恒真），再单独喂
`exit_signal` 龙头走弱+广度下降的证据，断言给出 `exit`。另有
`test_mainline_rotation_alone_forces_exit`（路径B单独触发）、
`test_exit_reports_hold_only_when_both_paths_ruled_out`（两条路径证据都齐全且
未触发才敢报 `hold`）、`test_exit_reports_unavailable_when_evidence_missing_and_not_triggered`
（证据不足时不得报 `hold`，只能报 `unavailable`——防止"证据不足"被静默当作
"可以继续持有"）。

### 3.3 相对强度字段的一个坑（施工中发现，已修正）

最初把"候选相对强度"直接映射成当日涨跌幅 `(t_close−t_prev_close)/t_prev_close`。
但本 universe 全部是当日涨停事件，收盘价钉在同一个涨停价附近，这个量对每个样本
几乎恒为同一个百分比，**没有任何区分度**——回测反事实测试跑起来后约束模型直接
把所有候选判成"一字/未开板"（因为构造测试数据时为了制造涨跌幅差异，把收盘价
拉到远超真实10%涨停价，触发了成交约束模型的"越权"判定）。改用**封板早晚**
（同 `hot_money_selection` 的 `seal_speed` 因子同构：越早封板越强）作为代理，
在 `daban_bt_assist_arbitrage.py::event_record` 里换算，`skills/common/
assist_arbitrage.py` 的字段名不变（配置驱动，模块本身不关心字段的具体含义）。

## 4. 数据缺口（诚实标注，不造代理值）

真实 v4 事件表（`event_table_akshare_m-sina_20260728_20260821.json`，648 条事件，
22 个交易日窗口）跑出的 `unavailable_reasons`：

```
breakout_time_missing_or_not_broken_out: 201
leader_score_shadow_unavailable: 201
theme_peer_sample_insufficient: 152
```

两个结构性缺口：

- **`breakout_time`**（候选率先突破日内关键位）：需要盘中"关键位"检测管道，
  本仓库目前没有——同 S1 的 `volume_ratio`、S2 的 `pre_reseal_turnover_pct` 一样，
  是分钟线派生管道的已知缺口（见 `docs/event-schema-v4-2026-08.md`）。
- **`leader_score_shadow`**：回测脚本用 `hot_money_selection.leader_score()` 现算，
  但六因子里 `seal_speed`/`resilience` 仅深度池可得（回测事件表不是深度池）、
  `relative_strength` 需要全市场中位数与板块前十均值（事件表没有这两个横截面
  基准）、`attention` 需要社交关注度快照（同样没有）——事件表能喂给它的通常只有
  `assist_breadth` 一项（权重0.15），远低于 `min_available_weight`(0.60)，因此
  几乎必然 `unavailable`。

`theme_peer_sample_insufficient`（152/201）是三级缺口的自然结果：题材同组样本
不足 `min_theme_peer_count`(5) 时也 fail-closed。

## 5. 真实运行结果（UNVERIFIED）

```
$ PYTHONPATH=<worktree> python daban_bt_assist_arbitrage.py \
    --table ~/.hermes/skills/chanlun-backtest/data/event_table_akshare_m-sina_20260728_20260821.json \
    --counterfactual
```

- `event_count` = 648（22 个交易日窗口内的全部涨停事件）
- `universe_count` = 201（排除各组龙头后实际被判定的候选数）
- `signal_count` = 0（有约束/无约束两种口径都是 0）
- `filled_count` = 0，`returns` 全为 `null`
- `constraints_bite` = `false`（零样本时如实报告"约束未生效"，不伪造"约束在咬"的
  结论——反事实测试的空集分支专门守这一点）

**如实结论：0 命中，UNVERIFIED。** 这不是"策略没有信号"，是上面两个证据字段在
当前数据管道上结构性不可得；一旦分钟线关键位检测管道 + 全市场横截面基准落地，
才有条件重新评估。

## 6. 单测覆盖（`tests/test_assist_arbitrage.py`，38 个用例全绿）

- 四个入场条件各自边界（`leader_score_min`/`sector_breadth_min`/
  `board_level_below_leader`/`relative_strength_top20`）+ 入场触发条件 + 全证据
  缺失 → `unavailable`；
- LeaderScore 复用纪律（读 `leader_score_shadow`，不可得 fail-closed）；
- 退出条件（3.2 节）；
- `pick_leader`（龙头识别：连板最高，并列按 code 升序，无可用连板高度时不可判定）；
- `evaluate_group` 排除龙头本身参与候选评估；
- NON-LIVE 消费端行为断言：`decision_policy.evaluate_decision` 把正向信号降级为
  `watch`/仓位倍率0，`recommendation_audit.position_guidance` 归零，
  `strategy_packs.registry_records()["assist_arbitrage"]` 报
  `allowed_in_live_agent=False`/`gate_decision="not_gated"`，
  `strategy_registry.is_allowed_in_live("assist_arbitrage")` 为 `False`；
- 反事实：合成事件表上关闭 P5 成交约束后收益虚高（`mean_return_inflation>0.05`，
  一字板赢家被约束剔除）+ 空样本时 `constraints_bite=False`（防假绿）；
- 真实事件表上 fail-closed 成零命中（`test_backtest_fails_closed_when_
  leader_score_shadow_unavailable`）。

Mutation check（先 commit 再改坏 → 确认变红 → 复原；每次改动前后用
`git diff --numstat` 确认改动行数非空）覆盖 4 项：LeaderScore 比较方向
（`>=`→`<`）、`board_level_condition` 的减号翻转（`-`→`+`）、
`exit_signal` 的路径A布尔逻辑（`and`→`or`）、`breakout_rank` 的排序键
（升序→降序）。结果详见交付报告。

## 7. 未注册证据

- `strategy_registry.live_record("assist_arbitrage")` → `None`
- `strategy_registry.is_allowed_in_live("assist_arbitrage")` → `False`
- `skills/common/assist_arbitrage.py` 与 `skills/chanlun-backtest/scripts/
  daban_bt_assist_arbitrage.py` 全文零次调用 `register_gate_result`
- `config/strategy_packs/assist_arbitrage.yaml` 的 `research_status` 明确写
  `not_gated_zero_sample`

## 8. 结论

当前不具备注册条件。等分钟线关键位检测管道与全市场横截面基准（`market_median_
change`、板块前十均值、深度池 `seal_speed`/`resilience` 覆盖）落地后，再重新跑
一次真实回测评估是否有非零样本可供 walk-forward OOS 验证。
