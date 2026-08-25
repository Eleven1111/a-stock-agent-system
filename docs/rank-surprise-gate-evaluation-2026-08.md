# S1 超预期（RankSurprise）闸门评估报告（2026-08-25）

对应 `docs/hot-money-emotion-system-upgrade-plan-2026-08.md` §6（P3）的第一个策略 S1。
格式参照 `docs/chanlun-gate-evaluation-2026-08.md`。

> **结论先行：当前不具备注册条件。** β₁/β₂ 未拟合、本机无全市场日线缓存、既有事件表
> 不含 09:45 前量比 —— 真实样本数为 **0**，因此本报告**不含也不允许出现**任何胜率 /
> PF / 期望值 / 最大回撤数字。S1 保持 pack NON-LIVE、`strategy_registry` 缺席。
> 零样本如实报 UNVERIFIED 是本轮的正确结局，不是失败。

---

## 1. 评估对象与信号定义

| 项 | 值 |
|---|---|
| strategy_id（若将来注册） | `rank_surprise` |
| pack | `config/strategy_packs/rank_surprise.yaml`（NON-LIVE） |
| 信号实现 | `skills/common/rank_surprise.py`（纯函数，不触网） |
| 回测接线 | `skills/chanlun-backtest/scripts/daban_bt_rank_surprise.py` |
| 阈值单一事实源 | `config/daban_thresholds.yaml` 的 `rank_surprise` 节（**新增节**，未改任何既有入场/过滤阈值） |
| 注册状态 | **未注册**（见 §7 证据） |

### 1.1 预期基准（本策略的成败点）

研究报告把「超预期」列为全书最通用的信号（弱→强、分歧→一致），同时警告它**最容易
事后解释**——"今天涨了所以是超预期"。因此实现的核心不是入场四条件，而是那条事前
可算、可证伪的基准：

```
ExpectedGap_i = Median(Gap_peer) + β₁·昨日收益% + β₂·连板高度
Surprise_i    = ActualGap_i − ExpectedGap_i
```

peer = 同板块、同交易日的梯队（不含自身）。**判据**：两个 ActualGap 完全相同、只有
peer 分布不同的标的，Surprise 必须不同；若相同，说明基准形同虚设。该判据由
`tests/test_rank_surprise.py::test_same_actual_gap_different_peers_yields_different_surprise`
守住（M3 变异确认它会变红）。

### 1.2 入场四条件与阈值表

| # | 条件 id | 判据 | 配置键 | 默认值 |
|---|---|---|---|---|
| 1 | `prior_rank_bottom` | 昨日板块内强度排名后 N% | `prior_rank_bottom_pct` | 0.30 |
| 2 | `auction_rank_top` | 今日竞价强度进板块内前 M% | `auction_rank_top_pct` | 0.20 |
| 3 | `volume_ratio` | 09:45 前量比 **>** 阈值（严格大于） | `min_volume_ratio` | 1.5 |
| 4 | `theme_not_ebbing` | 市场/题材 S 状态不在退潮集合内 | `ebbing_states` | `["S6"]` |

预期基准相关：`beta_prior_return` = 0.0、`beta_board_height` = 0.0、
`betas_fitted` = **false**、`min_peer_count` = 5。
β 置 0 是**占位**（此时基准退化为 peer 中位数），不是拟合结论；所有信号结果都会带
`degraded: ["betas_unfitted_placeholder"]` 标记，随结果传播到本报告。

强度排名口径：分位 = 板块内严格更弱者占比，∈ [0,1]，0 = 最弱，并列取同分位；样本
< 2 时分位为 None（单点排名无意义）。打板 universe 里昨日收益几乎全是 +10%，并列由
「封板时间早晚」做 tiebreak（封得越早越强），该 tiebreak 在回测适配层由
`first_seal` 映射。

题材退潮判定**复用**既有 `market_cycle_state` / `market_temperature` 的 S 状态口径
（`{available, dominant_state}`），未重造温度或周期判定。

---

## 2. 数据依赖与降级规则（fail-closed）

只消费已固化快照，**不新增任何网络请求**：09:24 全市场轻量竞价快照（ActualGap）、
昨日梯队（昨日收益 / 连板高度 / 封板时间）、板块映射（peer 分组键）。

| 缺失项 | 行为 | reason |
|---|---|---|
| 板块映射缺失 | `unavailable` | `sector_missing` |
| peer 数 < `min_peer_count` | `unavailable` | `peer_sample_insufficient` |
| peer gap 样本不足 | `unavailable` | `expected_gap:peer_gap_sample_insufficient` |
| 昨日收益 / 连板高度缺失 | `unavailable` | `expected_gap:prior_return_pct_missing` 等 |
| 竞价强度缺失 | `unavailable` | `expected_gap:actual_gap_missing` |
| 量比缺失 | `unavailable` | `volume_ratio_missing` |
| 市场/题材状态不可用 | `unavailable` | `theme_state_unavailable` |

**绝不返回 0，也绝不返回"默认不超预期"**：把"没数据"折叠成"不触发"，会让零样本看
起来像已验证的负结果——这正是假绿的一种。M4 变异（缺量比按通过处理）确认三条用例
会变红。

**量比口径缺口（诚实标注）**：方案要求「09:45 前量比」。既有事件表只有日线全日
volume，二者不是一回事，因此回测适配层**不造代理值**，事件表不带 `volume_ratio`
时一律 `unavailable`。若将来注入非盘中口径的量比，结果会带
`degraded: volume_ratio_source=...` 标记。

---

## 3. 回测口径

- 引擎：既有 `daban_bt_engine`（universe = 主板 10cm 涨停事件，按事件日期取当时制度）。
- **成交约束：走 P5(a) `skills/common/execution_constraints.py`** —— 一字涨停全日未
  开板禁买、回封按参与率部分成交（成交额不足阈值判买不进）、跌停无承接量拒卖顺延、
  滑点分档。
- 费用：既有 `DEFAULT_COST`（佣金 0.00025 / 印花税 0.0005 / 滑点 0.002）。
- T+1：`hold_mode` 三个变体沿用引擎既有语义；本报告默认 `board_overnight`
  （买 T 收、卖 T+1 收），一字封死实际买不进由约束模型剔除。
- 反事实对照：`--counterfactual` 在同一进程内跑「约束开 / 约束关」两套，报告收益虚高
  幅度（为此给 `daban_bt_engine.strategy_returns` 加了可选 `config` 参数，默认行为不变）。

### 3.1 反事实结果（合成事件表，方案 §6.2 第 1 条）

8 个同板块合成事件，S1 命中 2 个：一个是回封板（约束下可成交，次日 +1%），一个是
T 日一字板（约束下买不进，次日 +20%）。真实 CLI 运行输出：

| 口径 | 命中数 | 可成交数 | 均值净收益 |
|---|---|---|---|
| 约束**开**（可执行口径） | 2 | 1 | +0.4961% |
| 约束**关**（仅对照，不可执行） | 2 | 2 | +9.9487% |

`excluded_by_constraints = 1`，`mean_return_inflation = +9.4526pp`，
`constraints_bite = true`。**关掉约束后收益虚高约 20 倍**，证明约束真的在咬，
不是装饰。该对照同时有独立用例守着，并附样本非空断言（空集下"约束生效"恒真，
是假绿的经典来源；另有一条用例断言零样本时 `constraints_bite` 必须为 false）。

---

## 4. 真实样本数（真实运行，非合成）

用本机已固化事件表实跑 CLI，结果如下：

| 事件表 | schema | 事件数 | 进 universe | **命中 S1** | unavailable | 主因 |
|---|---|---|---|---|---|---|
| `event_table_20260528_20260602.json` | `..._v1` | 333 | 305 | **0** | 305 | `volume_ratio_missing` 305 / `prior_return_pct_missing` 305 |
| `event_table_mootdx_20240601_20260601.json` | `..._v1` | 40109 | 27110 | **0** | 27110 | `sector_missing` 27110 / `volume_ratio_missing` 27110 |

**真实事件数 = 0。** 两张表都是 v1 schema：不含 `t_prev_close`（算不出昨日收益）、
不含 09:45 前量比；mootdx 表还整表缺板块映射（peer 分不了组）。
本机 `sentiment_daily` 数据集目录不存在（0 行），全市场日线缓存覆盖率约 1.15%，
**walk-forward OOS（3 年训练 + 1 年验证滚动，N≥100）在本机跑不了**。

---

## 5. UNVERIFIED 清单（逐条，未验证就是未验证）

1. **UNVERIFIED — 策略是否有 edge**：真实样本 0，无胜率 / PF / 期望值 / MaxDD /
   Expectancy 任何数字。本报告不提供，将来也不得从子集数据里凑。
2. **UNVERIFIED — β₁/β₂ 的值**：未在任何 IS 段拟合，当前 0.0 为占位。基准因此退化为
   纯 peer 中位数，"昨日收益 / 连板高度修正"这一半从未被检验。
3. **UNVERIFIED — 09:45 前量比条件**：既有事件表没有该字段，条件 3 在真实数据上
   **一次都没被执行过**；只有合成 fixture 覆盖了它的边界（1.5 不通过 / 1.51 通过）。
4. **UNVERIFIED — 昨日强度排名口径**：打板 universe 里昨日收益几乎全部并列 +10%，
   实际排名由封板时间 tiebreak 决定。这个替代口径是否等价于方案说的「板块内强度
   排名」，未经任何实证。
5. **UNVERIFIED — 分情绪状态 PnL 拆解**（方案 §6.2 第 3 条）：零样本，无法判断
   "只在萌芽/发酵/分歧修复阶段有效"这一声称，因而也无法据此决定是否删掉情绪过滤。
6. **UNVERIFIED — GapRisk / LimitDownRisk 上报**（方案 §6.2 第 2 条）：本轮未接线。
7. **未做（非本轮范围）**：walk-forward OOS 脚本、消融报告 A→G、纸面交易 ≥20 笔。

---

## 6. 变异检查（先 commit 再变异；逐项改坏 → 确认变红 → 复原）

| # | 变异点 | 改法 | 结果 |
|---|---|---|---|
| M1 | `daban_bt_engine.strategy_returns` | 忽略传入的 `config`，永远读全局约束 | 🔴 `test_counterfactual_disabling_constraints_inflates_returns` |
| M2 | `execution_constraints.assess_buy_fill` | 一字涨停日放行买入 | 🔴 本策略 1 条 + 既有 `test_execution_constraints` 2 条 |
| M3 | `rank_surprise.expected_gap` | 基准去掉 `Median(Gap_peer)` 项 | 🔴 3 条（含防事后解释与"基准起作用"两条） |
| M4 | `rank_surprise._evidence_conditions` | 量比缺失按"满足"处理 | 🔴 3 条（含回测 fail-closed 用例） |
| M5 | `rank_surprise.evaluate` | 删掉 `min_peer_count` 下限判定 | 🔴 `test_peer_group_too_small_is_unavailable`（注：status 仍为 unavailable，因 `expected_gap` 自带的 peer 下限兜底；变红的是"缺的是哪条 reason"这一断言。两层下限是有意的纵深，不是重复） |
| M6 | `decision_policy.evaluate_decision` | 未注册策略（`strategy_record is None`）放行 | 🔴 `test_unregistered_signal_is_downgraded_to_watch_by_decision_policy` |
| M7 | `recommendation_audit.position_guidance` | 去掉实盘门控判定 | 🔴 `test_unregistered_signal_gets_zero_position_from_position_guidance` |

7 项全部确认变红后复原，工作区无残留（`git status` 干净）。

---

## 7. NON-LIVE 状态证据

- `strategy_registry` 中**没有** `rank_surprise`：`strategy_registry.live_record("rank_surprise")`
  返回 `None`，`is_allowed_in_live("rank_surprise")` 返回 `False`；生产 state home 下
  `strategy_registry.json` 文件本身不存在。新增代码里没有任何
  `register_gate_result` 调用。
- pack 视图：`strategy_packs.registry_records()["rank_surprise"]` →
  `allowed_in_live_agent: false`、`gate_decision: "not_gated"`，`score_hints: []`。
- **消费端行为断言（不是配置字段断言）**：构造真实的 S1 正向信号后断言
  1) `decision_policy.evaluate_decision(requested_action="buy", ...)` 实际输出
     `decision="watch"`、`position_multiplier=0.0`、`abstain=True`、
     reason 含 `strategy_unverified`；
  2) `recommendation_audit.position_guidance("rank_surprise", ...)` 实际输出
     `recommended_position_pct=0.0`、`recommended_amount=0.0`、`method="research_only"`。
  仅断言 pack 里 `live: false` 这类字段值**不算**通过（仓内黑名单：配置断言 ≠ 行为
  断言，auction-finalize 的死配置就是这么活了几个月的）。

---

## 8. 结论与恢复判据

**当前不具备注册条件。** 恢复推进 S1 的判据（全部满足才谈闸门）：

1. 事件表能提供 **09:45 前量比** 与 `t_prev_close`（昨日收益），且板块映射非空；
2. β₁/β₂ 在 IS 段完成拟合并落盘，`betas_fitted` 翻 true；
3. 全市场日线缓存覆盖率足以支撑 walk-forward OOS（N ≥ 100；30-100 之间只算案例观察）；
4. 通过 §6.2 的其余三条（分情绪状态 PnL 拆解、GapRisk/LimitDownRisk 上报、OOS 门槛）。

在此之前：pack 保持 NON-LIVE，registry 保持缺席，任何人不得把 `surprise` 值折进
`daban_score` / `trend_score` / 排序 / 仓位。
