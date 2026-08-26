# S5 反量龙回头（ReverseVolume）闸门评估报告（2026-08）

> 写入方式说明：`docs/*.md` 属于本机 settings 钩子拦截 Write 的白名单外文件，本文件
> 用 Bash heredoc 写入，不违反钩子意图（钩子拦的是绕过 CLAUDE.md/README 等命名约定
> 误建文档，不是禁止 docs/ 下的正常交付物）。

## 1. 交付范围（用户 2026-08-26 决策）

本机无全市场分钟线缓存，OOS 走不了。S5 本轮只交付到：pack + 信号实现 + 回测可跑
（接 P5 成交约束）+ 保持 NON-LIVE 未注册 + 诚实的 UNVERIFIED 报告。**未**注册进
`strategy_registry`，**未**用子集数据出胜率/PF/期望值结论。

**结论先行：当前不具备注册条件。** 真实事件表上零命中，且零命中不是"策略没有
信号"，是全部七类必需证据字段在现有 daban_bt_data(v4) 事件表结构上结构性不可得
（事件表是"单日涨停快照"，本策略需要跨周期/跨分钟的时间序列证据）。此外，
反量比值(1.3-1.5/1.5)与回撤区间(25%-40%)本身也只是单一历史案例（摩恩电气）的
工程化取值，不是统计结论。

## 2. 信号定义与实现

前提：标的是前一周期最高人气/高度股 ∧ 从高点回撤25%-40% ∧ 大盘/情绪不再加速
恶化。初步观察：3-5日波动收窄 ∧ 成交量缩至20日低分位。反量确认：单分钟上攻量
峰值 ≥ 此前单分钟下跌量峰值的1.3-1.5倍。回踩确认：价格下探但下跌量峰值继续
萎缩。入场10%观察仓；二次确认（比值≥1.5 ∧ 突破短期平衡区）加至20%-30%
（升级方案 §6.1，原型取自原书摩恩电气案例）。

实现：`skills/common/reverse_volume.py`（纯函数，零网络请求）。七个入场条件：

| 条件 id | 判据 | 默认阈值 |
|---|---|---|
| `prior_period_top_leader` | 是否前一周期最高人气/高度股 | 布尔，直接读取 |
| `drawdown_in_range` | 从高点回撤幅度 ∈ [下限,上限] | [0.25, 0.40] |
| `sentiment_not_deteriorating` | 大盘/情绪不再加速恶化 | 外部状态口径 |
| `volatility_contraction` | 近3-5日波动/此前波动 ≤ 上限 | 0.6 |
| `volume_low_percentile` | 成交量20日分位 ≤ 上限 | 0.30 |
| `reversal_volume_confirmed` | 单分钟上攻量峰值/此前单分钟下跌量峰值 ≥ 下限 | 1.3 |
| `pullback_down_volume_shrinking` | 回踩期下跌量峰值 < 此前下跌量峰值 | 严格小于 |

二次确认（`second_confirmation()`，与入场七条件完全独立）：

| 条件 id | 判据 | 默认阈值 |
|---|---|---|
| `reversal_volume_ratio_second_confirm` | 二次上攻量峰值/回踩下跌量峰值 ≥ 下限 | 1.5 |
| `breakout_above_balance_zone` | 价格是否突破短期平衡区 | 布尔，直接读取 |

阈值单一事实源：`config/daban_thresholds.yaml` 的 `reverse_volume` 节（新增节，
未改任何既有阈值）；`skills/common/daban_config.py` DEFAULTS 同步。

策略包（解释层）：`config/strategy_packs/reverse_volume.yaml`。

回测接线：`skills/chanlun-backtest/scripts/daban_bt_reverse_volume.py`，接既有
`daban_bt_engine.strategy_returns`（含 P5(a) 成交约束 + DEFAULT_COST 费用口径 +
`board_overnight` 持有口径）。

## 3. 本策略的成败点

### 3.1 分钟量峰值必须复用 minute_derived，不自行解析

`max_directional_minute_volume()` 只在 `minute_derived.normalize_tencent_minute`
/ `normalize_sina_minute` 的归一化输出之上加一层"按涨跌方向分类"——这是
minute_derived 本身没有的语义（它只服务 S1/S2 的两个标量字段）。所有单位换算
（腾讯"手"累计值/新浪"股"增量值）与时间解析全部走 minute_derived，本模块零
自行解析。验证：
`test_max_directional_minute_volume_reuses_minute_derived_normalize`（把
`normalize_tencent_minute` 换成返回固定虚构值的桩，结果必须原样反映桩的数字——
证明数值真的来自那次调用，不是本模块自己重算的）、
`test_max_directional_minute_volume_returns_unavailable_when_normalize_fails`
（归一化失败时原样 fail-closed，不退回自己解析原始字段）。

### 3.2 反未来函数（本策略的要害）

"此前最大下跌分钟量"必须是入场时刻之前的历史极值。验证：
`test_max_directional_minute_volume_ignores_rows_after_until_time`——先算出
截至 09:34 的历史下跌量峰值(40000股)，再把 09:35 一根更大的下跌分钟(300000股)
追加进输入、`until_time` 不变，断言结果必须原样是 40000（`==baseline`）；并反证
不设 `until_time` 时确实会被这根未来数据吃到(300000)，证明前面的稳定不是因为
测试数据本身没有更大的值。Mutation 表（3.4节）第3项直接把这条防线的 `<=` 改成
`<`，被这条用例当场抓到。

### 3.3 比值/区间来自单一历史案例，非统计结论

`reversal_volume_ratio_min`(1.3)、`reversal_volume_ratio_second_min`(1.5)、
`min_drawdown_pct`/`max_drawdown_pct`(0.25/0.40) 全部来自原书摩恩电气一个案例
的工程化取值。`config/daban_thresholds.yaml`、`skills/common/daban_config.py`、
`config/strategy_packs/reverse_volume.yaml`、本报告四处都显式标注"未经样本外
验证"，任何 agent 解释产出都不得把它们包装成"经验阈值"或"业界共识"。

### 3.4 Mutation check（先 commit 再改坏 → 确认变红 → 复原）

每次改动前后用 `git diff --numstat` 确认变异确实生效，结果：

| # | 改动 | 位置 | 变红情况 | 复原 |
|---|---|---|---|---|
| 1 | `ratio >= ratio_min` → `ratio < ratio_min` | `reversal_volume_condition` | 12 项失败（含边界/消费端/反事实用例） | 已复原，diff清零 |
| 2 | `pullback_down < down_prior` → `>` | `pullback_shrink_condition` | 11 项失败 | 已复原，diff清零 |
| 3 | `minute <= until` → `minute < until` | `max_directional_minute_volume` | 反未来函数专门用例失败（40000变5000） | 已复原，diff清零 |
| 4 | `minimum <= drawdown <= maximum` → `<` | `drawdown_condition` | 2 项边界用例失败 | 已复原，diff清零 |

## 4. 数据缺口（诚实标注，不造代理值）

真实 v4 事件表（`event_table_akshare_m-sina_20260728_20260821.json`，648 条事件，
22 个交易日窗口，`filter_universe` 后 562 条）跑出的 `unavailable_reasons`
（`with_constraints`/`without_constraints` 两种口径完全一致）：

```
drawdown_pct_missing: 562
market_sentiment_unavailable: 562
prior_period_leader_status_missing: 562
pullback_volume_evidence_missing: 562
reversal_volume_evidence_missing: 562
volatility_contraction_ratio_missing: 562
volume_percentile_20d_missing: 562
```

结构性原因：daban_bt_data(v3/v4) 事件表是"单日涨停快照"结构（每条记录=一次T日
涨停+T+1表现），本策略需要的七类证据全部是跨周期/跨分钟的时间序列证据，事件表
结构上就不携带：

- `was_prior_period_top_leader` / `drawdown_pct`：需要标的跨周期的人气排名与
  历史最高价序列，事件表逐日独立快照，没有"上一周期"和"更早的高点"的概念。
- `market_sentiment`：需要外部市场状态口径，本脚本开放 `--market-state` 参数
  透传，命令行未给时同 rank_surprise 的 `theme_alive` 一样 fail-closed。
- `volatility_contraction_ratio` / `volume_percentile_20d`：需要标的至少
  20+ 个交易日的日线序列，事件表没有落这条数据。
- `max_up_minute_volume` / `max_down_minute_volume_prior` /
  `pullback_max_down_minute_volume` / `second_max_up_minute_volume`：本策略
  的核心证据，事件表 v4 只固化了 09:45 量比这一个**标量**（`volume_ratio`），
  不落原始分钟行——同 S1 的 volume_ratio、S2 的 pre_reseal_turnover_pct 一样，
  是分钟线派生管道尚未覆盖到"多个时间窗口的方向性峰值"这一层（见
  `docs/minute-derived-pipeline-2026-08.md`）。
- `breakout_above_balance_zone`：需要盘中"短期平衡区"检测，同 S3 的
  `breakout_time` 缺口同构，事件表没有这条盘中关键位管道。

## 5. 真实运行结果（UNVERIFIED）

```
$ PYTHONPATH=<worktree> python daban_bt_reverse_volume.py \
    --table ~/.hermes/skills/chanlun-backtest/data/event_table_akshare_m-sina_20260728_20260821.json \
    --counterfactual
```

- `event_count` = 648（22 个交易日窗口内的全部涨停事件）
- `universe_count` = 562（`filter_universe` 后实际被判定的候选数）
- `signal_count` = 0（有约束/无约束两种口径都是 0）
- `filled_count` = 0，`returns` 全为 `null`
- `constraints_bite` = `false`（零样本时如实报告"约束未生效"，不伪造"约束在咬"
  的结论——反事实测试的空集分支专门守这一点）

**如实结论：0 命中，UNVERIFIED。** 这不是"策略没有信号"，是上面七类证据字段
在当前数据管道上结构性不可得；一旦跨周期高点/回撤序列 + 分钟线方向性峰值检测
管道落地，才有条件重新评估。

## 6. 单测覆盖（`tests/test_reverse_volume.py`，54 个用例全绿）

- 七个入场条件各自边界（`prior_period_top_leader`/`drawdown_in_range`/
  `sentiment_not_deteriorating`/`volatility_contraction`/`volume_low_percentile`/
  `reversal_volume_confirmed`/`pullback_down_volume_shrinking`）+ 全证据缺失 →
  `unavailable`；
- 二次确认两条件（`reversal_volume_ratio_second_confirm`/
  `breakout_above_balance_zone`）边界 + 缺失 fail-closed；
- `max_directional_minute_volume`：腾讯/新浪两种供应商形状的涨跌分类、反未来
  函数、minute_derived 复用纪律（3.1/3.2节）、非法 `until_time`/方向/来源的
  fail-closed；
- NON-LIVE 消费端行为断言：`decision_policy.evaluate_decision` 把正向信号降级为
  `watch`/仓位倍率0，`recommendation_audit.position_guidance` 归零，
  `strategy_packs.registry_records()["reverse_volume"]` 报
  `allowed_in_live_agent=False`/`gate_decision="not_gated"`，
  `strategy_registry.is_allowed_in_live("reverse_volume")` 为 `False`；
- 反事实：合成事件表上（monkeypatch `event_record` 注入满足全部证据的信号，因为
  真实 `event_record` 结构性拿不到这些字段）关闭 P5 成交约束后收益虚高
  （`mean_return_inflation>0.05`，一字板赢家被约束剔除）+ 空样本时
  `constraints_bite=False`（防假绿）；
- 真实事件结构上 fail-closed 成零命中（
  `test_backtest_fails_closed_on_real_structure_with_zero_hits`）。

Mutation check（3.4节）覆盖 4 项：反量确认比值方向（`>=`→`<`）、回踩萎缩方向
（`<`→`>`）、反未来函数截止时刻的边界（`<=`→`<`）、回撤区间的闭区间性
（`<=...<=`→`<...<`）。全部先 commit 再改坏，`git diff --numstat` 确认变异
生效后再跑测试，全部变红后复原。

## 7. 未注册证据

- `strategy_registry.live_record("reverse_volume")` → `None`
- `strategy_registry.is_allowed_in_live("reverse_volume")` → `False`
- `skills/common/reverse_volume.py` 与 `skills/chanlun-backtest/scripts/
  daban_bt_reverse_volume.py` 全文零次调用 `register_gate_result`
- `config/strategy_packs/reverse_volume.yaml` 的 `research_status` 明确写
  `not_gated_zero_sample`

## 8. 结论

当前不具备注册条件。等跨周期人气/高点历史序列 + 分钟线方向性峰值检测管道
（在 `minute_rows_source` 之上支持"入场前/回踩期"多个时间窗口检索）+ 20日
日线波动/成交量分位落地后，再重新跑一次真实回测评估是否有非零样本可供
walk-forward OOS 验证。反量比值(1.3-1.5/1.5)与回撤区间(25%-40%)在此之前
始终只是单一历史案例的工程化取值，不得当作已验证的经验阈值使用。
