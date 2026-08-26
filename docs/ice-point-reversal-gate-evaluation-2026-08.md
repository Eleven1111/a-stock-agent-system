# S6 冰点反转（IcePointReversal）闸门评估报告（2026-08）

> 写入方式说明：`docs/*.md` 属于本机 settings 钩子拦截 Write 的白名单外文件，本文件
> 用 Bash heredoc 写入，不违反钩子意图（钩子拦的是绕过 CLAUDE.md/README 等命名约定
> 误建文档，不是禁止 docs/ 下的正常交付物）。

## 1. 交付范围

S6 本轮只交付到：pack + 信号实现 + 回测可跑（接 P5 成交约束）+ 保持 NON-LIVE
未注册 + 诚实的 UNVERIFIED 报告。**未**注册进 `strategy_registry`，**未**用小
样本出胜率/PF/期望值结论。

**结论先行：当前不具备注册条件，且比其他 S1-S5 策略更严格。** 升级方案 §6.1
明确写明 S6"依赖 P1 校准结论支持后才启动回测"。P1（State PnL 分阶段收益归因，
#269）在本机只有 3 条结算样本，是**零样本 UNVERIFIED**——情绪状态是否真有区分度
既未证实也未证伪。因此 S6 不是"先接线、等真实数据积累后再评估"（S1-S5 的路径），
而是必须先等 P1 在 full 模式下产出覆盖样本、且分档单格 n>=30，才具备启动
research_gate 的前提条件。本轮交付的管道本身**不构成**"P1 前置已满足"的证据。

## 2. 信号定义与实现

原书专门批评过的最容易被误用的模式：

```
炸板率很高 + 涨停少 + 跌停多 = 明天抄底   ← 绝对不是
```

正确阶段序列：A 极端亏钱效应 → B 继续恐慌但恶化速度下降 → C 逆势活口出现
→ D 涨停溢价/涨跌比/炸板率至少两项改善 → E 活口被市场确认 + 板块扩散 → 才买入。

量化谓词（四项**全部**满足才触发）：

```
Signal = (S_{t-1} < 20) ∧ (ΔS_t > 10) ∧ (LeaderConfirm = 1) ∧ (SectorBreadth >= 3)
```

实现：`skills/common/ice_point_reversal.py`（纯函数，零网络请求）。四项条件：

| 条件 id | 判据 | 阈值来源 |
|---|---|---|
| `prev_score_extreme_below_threshold` | S_t-1 < prev_score_max | scoring.yaml sentiment_score.ice_confirm（默认20） |
| `delta_score_improving_above_threshold` | ΔS_t > delta_min | 同上（默认10） |
| `leader_confirm` | 逆势活口被市场确认 | 布尔，直接读取 |
| `sector_breadth_at_least_threshold` | 板块扩散广度 >= sector_breadth_min | 同上（默认3） |

**S_t/ΔS_t 全部来自 `skills/common/sentiment_score.py`（P0，#267）的
`compute_sentiment_score()`，本模块不重新实现滚动分位/加权评分**。阈值单一
事实源是 `config/scoring.yaml` 的 `sentiment_score.ice_confirm` 子节——
`config/daban_thresholds.yaml` **没有**新增 `ice_point_reversal` 节，避免
制造第二份阈值拷贝（CLAUDE.md 黑名单 A 组"用配置断言替代行为断言"的反面
教训：多一份配置就多一处"谁说了算"的分歧风险）。`sentiment_score.py` 本身
已经实现了同构的组合判定 `ice_point_confirmed()`；本模块拆成 4 个独立
condition 条目并区分"数据缺失"与"条件不满足"，是为了让合取纪律可以被逐条
断言，不是为了重算数值。

策略包（解释层）：`config/strategy_packs/ice_point_reversal.yaml`。

回测接线：`skills/chanlun-backtest/scripts/daban_bt_ice_point_reversal.py`，
接既有 `daban_bt_engine.strategy_returns`（含 P5(a) 成交约束 + DEFAULT_COST
费用口径 + `board_overnight` 持有口径）。S_t/ΔS_t/SectorBreadth 是市场级状态，
同一天所有候选共享同一份证据，按 S5 的 `market_state` 传参方式实现，不在每条
候选记录里重复携带。

## 3. 本策略的成败点

### 3.1 合取纪律（本策略的要害）

单独满足"冰点"（S_t-1<20）永不触发买入——四项必须全部满足，缺一不可。原书
记录过一名交易者机械在冰点打高度板、两周大赚后连续大面回撤 30%+ 的教训："否极
并不必然泰来"。验证：

- `test_full_conjunction_produces_signal`：四项全满足才是 signal；
- `test_dropping_any_single_condition_prevents_signal`（4 条独立参数化用例）：
  逐项去掉任一项，其余三项不变，结果必须变成 no_signal 而不是 signal；
- `test_ice_point_alone_does_not_trigger_signal`：专门构造"冰点条件本身满足
  (S_t-1=5<20)，其余三项（ΔS_t/LeaderConfirm/SectorBreadth）都不满足"这个
  最容易被误用的场景，断言结果是 no_signal，且逐条件断言冰点条件确实为
  True——证明不是因为冰点条件本身没触发才没signal，而是合取纪律真的在拦。

Mutation 表（3.3节）第2项直接把 `all(...)` 改成 `any(...)`（合取→析取），
被上面 9 条用例当场抓到，是本轮最关键的一条防线。

### 3.2 sentiment_score 复用纪律 + fail-closed

- `test_prev_and_delta_come_from_sentiment_score_stub`：把
  `sentiment_score.compute_sentiment_score` 换成返回固定虚构值(3.5/42.0)的桩，
  结果的 `detail` 字段必须原样包含这两个数字——证明 S_t/ΔS_t 真的来自那次
  调用，不是本模块自己重算的；
- `test_sentiment_score_unavailable_status_propagates_as_unavailable_not_no_signal`：
  桩返回 `status=unavailable`（模拟预热不足180日等情形）时，S6 必须整体
  `unavailable`，绝不能折叠成"不是冰点"这个 no_signal 负面结论；
- `test_sentiment_score_config_missing_is_unavailable_not_no_signal`：
  `scoring.yaml` 的 sentiment_score 节缺失（`ss.load_config()`→None）时同样
  `unavailable`，且 prev/delta/breadth 三个条件的 `ok` 全部是 `None`（不可
  判定），不是 `False`（判定为不满足）——两者语义不同，混淆就是把"没数据"
  包装成"已验证的负结果"；
- `test_empty_sentiment_series_is_unavailable_via_real_module`：不打桩的
  真实路径，空情绪序列喂给真实的 `compute_sentiment_score`，同样
  `unavailable`。

Mutation 表第 4 项把 `leader_confirm_condition` 的 `value is None` 判空
改成 `not value`（会把显式 `False` 也误判成"缺失"），被 3 条用例抓到——
证明"数据缺失"与"条件为假"这两种状态在实现里确实是分开处理的，不是
巧合地都返回了同一个值。

### 3.3 Mutation check（先 commit 再改坏 → 确认变红 → 复原）

每次改动前后用 `git diff --numstat` 确认变异确实生效，结果：

| # | 改动 | 位置 | 变红情况 | 复原 |
|---|---|---|---|---|
| 1 | `prev < maximum` → `prev > maximum` | `prev_score_extreme_condition` | 9 项失败（含合取/边界/复用/消费端用例） | 已复原，diff清零 |
| 2 | `all(...)` → `any(...)` | `evaluate` 状态判定（合取→析取） | 9 项失败（含 4 条"逐项去掉"用例 + 冰点单独用例 + 4 条边界用例） | 已复原，diff清零 |
| 3 | `breadth >= minimum` → `breadth > minimum` | `sector_breadth_condition` | 7 项失败（含合取/边界/消费端用例） | 已复原，diff清零 |
| 4 | `value is None` → `not value` | `leader_confirm_condition` | 3 项失败（含"逐项去掉"/冰点单独/边界用例） | 已复原，diff清零 |

## 4. 数据缺口（诚实标注，不造代理值）

真实 v4 事件表（`event_table_akshare_m-sina_20260728_20260821.json`，648 条
事件，22 个交易日窗口，`filter_universe` 后 562 条）跑出的 `unavailable_reasons`
（`with_constraints`/`without_constraints` 两种口径完全一致）：

```
leader_confirm_missing: 562
sector_breadth_missing: 562
sentiment_score_unavailable: 562
```

结构性原因：daban_bt_data(v3/v4) 事件表是"单日涨停快照"结构，S6 需要的三类
证据全部是市场级/跨交易日证据，事件表结构上不携带：

- `sentiment_series`（S_t/ΔS_t 计算输入）：需要至少 180 个交易日的
  `sentiment_daily` 时间序列，本脚本开放 `--sentiment-table` 参数，命令行
  未给时该项证据整体 `None`——本次真实运行未提供该参数，`sentiment_score_
  unavailable` 反映的正是这个缺口。
- `leader_confirm`（逆势活口是否被市场确认）：需要跨标的的"新出现的反弹龙头
  是否被市场追认"这一判断，本机没有对应管道产出这个布尔量，同 S1 的
  `theme_alive`、S5 的 `was_prior_period_top_leader` 缺口同构。
- `sector_breadth_top`（板块扩散广度）：`sentiment_daily.sector_breadth_top()`
  已经产出这个派生字段，但需要随 `--sentiment-table` 一起提供才能读到；单独
  跑事件表拿不到。

## 5. 真实运行结果（UNVERIFIED）

```
$ PYTHONPATH=<worktree> python daban_bt_ice_point_reversal.py \
    --table ~/.hermes/skills/chanlun-backtest/data/event_table_akshare_m-sina_20260728_20260821.json \
    --counterfactual
```

- `event_count` = 648（22 个交易日窗口内的全部涨停事件）
- `universe_count` = 562（`filter_universe` 后实际被判定的候选数）
- `signal_count` = 0（有约束/无约束两种口径都是 0）
- `filled_count` = 0，`returns` 全为 `null`
- `constraints_bite` = `false`（零样本时如实报告"约束未生效"，不伪造"约束在咬"
  的结论——反事实测试的空集分支专门守这一点）

**如实结论：0 命中，UNVERIFIED。** 这不是"策略没有信号"，是上面三类证据字段
在当前数据管道上结构性不可得；且即便三类证据齐全，P1 前置依赖仍未满足，
即使跑出非零样本也不构成可用于注册决策的证据（见第1节）。

## 6. 单测覆盖（`tests/test_ice_point_reversal.py`，31 个用例全绿）

- 合取纪律（3.1节）：全满足触发 + 逐项去掉任一项不触发（4条独立用例）+
  单独冰点不触发；
- 四个条件各自边界（`prev_score_extreme_below_threshold`/
  `delta_score_improving_above_threshold`/`sector_breadth_at_least_threshold`
  的严格/闭区间边界、`leader_confirm` 布尔直读）+ 全证据缺失 → `unavailable`；
- sentiment_score 复用纪律（3.2节）：monkeypatch 桩验证数值来源、
  status=unavailable/config缺失/空序列三种情形均 fail-closed 成 unavailable
  而非 no_signal；
- NON-LIVE 消费端行为断言：`decision_policy.evaluate_decision` 把正向信号
  降级为 `watch`/仓位倍率0，`recommendation_audit.position_guidance` 归零，
  `strategy_packs.registry_records()["ice_point_reversal"]` 报
  `allowed_in_live_agent=False`/`gate_decision="not_gated"`，
  `strategy_registry.is_allowed_in_live("ice_point_reversal")` 为 `False`，
  新增源码零次 `register_gate_result` 调用；
- 反事实：合成事件表上（monkeypatch `ipr.evaluate` 注入全部满足的信号，因为
  真实 `event_record` 结构性拿不到市场级证据）关闭 P5 成交约束后收益虚高
  （`mean_return_inflation>0.05`，一字板赢家被约束剔除）+ 空样本时
  `constraints_bite=False`（防假绿）；
- 真实事件结构上 fail-closed 成零命中
  （`test_backtest_fails_closed_on_real_structure_with_zero_hits`）；
- `build_market_state` 按 `as_of_date` 正确切片情绪序列（反未来函数纪律：
  事件日之后的情绪记录不得混入）。

Mutation check（3.3节）覆盖 4 项：S_t-1 冰点方向判定（`<`→`>`）、合取→析取
（`all`→`any`，本策略最关键的一条防线）、SectorBreadth 闭区间性
（`>=`→`>`）、LeaderConfirm 缺失判定的类型纪律（`is None`→`not value`）。
全部先 commit 再改坏，`git diff --numstat` 确认变异生效后再跑测试，全部
变红后复原。

## 7. 未注册证据

- `strategy_registry.live_record("ice_point_reversal")` → `None`
- `strategy_registry.is_allowed_in_live("ice_point_reversal")` → `False`
- `skills/common/ice_point_reversal.py` 与 `skills/chanlun-backtest/scripts/
  daban_bt_ice_point_reversal.py` 全文零次调用 `register_gate_result`
  （`test_no_register_gate_result_call_in_new_module_source` 用
  `inspect.getsource` 断言，不是配置断言）
- `config/strategy_packs/ice_point_reversal.yaml` 的 `research_status` 明确写
  `not_gated_zero_sample`，并显式说明 P1 前置依赖未满足

## 8. 结论

**当前不具备注册条件，且前置依赖比其他 S1-S5 策略更严格。** 升级方案 §6.1
明确要求 S6"依赖 P1 校准结论支持后才启动回测"——P1（#269）本机零样本
UNVERIFIED，情绪状态是否真有区分度既未证实也未证伪。因此 S6 的注册路径不是
"接线→等数据积累→评估"，而是：

1. 先等 P1 在 full 模式下产出覆盖样本，且分档单格 n>=30（而不是本机现有的
   3 条结算样本）；
2. 再补齐 `sentiment_series`（>=180交易日情绪日报）+ `leader_confirm`
   （逆势活口市场确认判定管道）+ `sector_breadth_top` 随 `--sentiment-table`
   一并提供的真实回测输入；
3. 才具备启动 research_gate walk-forward OOS 验证的前提条件。

在此之前，`config/scoring.yaml` 的 `sentiment_score.ice_confirm` 阈值
（20/10/3）与本模块的四项合取判定，都只是待检验假设，不得当作已验证的
经验阈值使用，更不得把"单独冰点"包装成买入信号——这正是原书批评的误用模式
本身。
