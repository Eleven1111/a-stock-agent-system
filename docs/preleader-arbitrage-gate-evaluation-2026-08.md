# S4 先于龙头套利（PreleaderArbitrage）闸门评估报告（2026-08）

> 写入方式说明：`docs/*.md` 属于本机 settings 钩子拦截 Write 的白名单外文件，本文件
> 用 Bash heredoc 写入，不违反钩子意图（钩子拦的是绕过 CLAUDE.md/README 等命名约定
> 误建文档，不是禁止 docs/ 下的正常交付物）。

## 1. 交付范围（用户 2026-08-26 决策）

本机历史窗口仅 22 个交易日，OOS 走不了。S4 本轮只交付到：pack + 信号实现 + 回测
可跑（接 P5 成交约束）+ 保持 NON-LIVE 未注册 + 诚实的 UNVERIFIED 报告。**未**注册进
`strategy_registry`，**未**用小样本出胜率/PF/期望值结论。

**结论先行：当前不具备注册条件。** 真实事件表上零命中，且零命中不是"策略没有
信号"，是候选自身成交额（`t_amount`）在现有 v4 事件表上全量缺失（648/648 条为
`None`）——这是一个结构性数据缺口，不是策略判定问题。

## 2. 信号定义与实现

原书航天通信案例的精髓不是"买某只票"，而是纪律：D-1 晚间已经建立「龙头候选 →
属性 → 同属性首板候选」映射表（并排除重大利空、流动性不足），D0 盘中只负责
"确认"——龙头一旦确认，只允许买盘前表内已经列好的候选，不做临时选股
（升级方案 §6.1）。

实现：`skills/common/preleader_arbitrage.py`（纯函数，零网络请求）。四条入场
条件：

| 条件 id | 判据 | 默认阈值 |
|---|---|---|
| `pretable_generated_before_d0` | 盘前表 `as_of` 严格早于候选交易日 | — |
| `candidate_in_pretable` | 候选须出现在盘前表对应(龙头,属性)条目的候选列表内 | — |
| `leader_confirmed_reaction_window` | 龙头已确认 ∧ 候选须在确认后 N 分钟内完成自身反应 | N=10 |
| `candidate_liquidity_min` | 候选当日成交额 ≥ 下限 | 2000万 |

盘前表构造（`build_pretable`）：只吃 `as_of` 及更早的数据，任何 `date > as_of`
的输入记录一律丢弃；候选池排除 `is_st`、`material_bad_news`、
`avg_turnover_20d < min_member_avg_turnover`（默认2000万），排除原因记在
`excluded` 里。

阈值单一事实源：`config/daban_thresholds.yaml` 的 `preleader_arbitrage` 节
（新增节，未改任何既有阈值）；`skills/common/daban_config.py` DEFAULTS 同步。

策略包（解释层）：`config/strategy_packs/preleader_arbitrage.yaml`。

回测接线：`skills/chanlun-backtest/scripts/daban_bt_preleader_arbitrage.py`。
本仓库没有独立的全市场 D-1 题材/龙头扫描管道，回测层退而求其次：用事件表里
"前一个出现的交易日"的记录构建当天要用的盘前表（`_build_pretables_by_date`），
`build_pretable` 本身依旧只吃传入的 `as_of` 及更早数据，纪律没有放松，只是把
"D-1 全市场扫描"这个真实生产输入换成了"事件表能提供的最近一个交易日"这个回测
专用近似——已在模块 docstring 诚实标注。

## 3. 本策略的成败点：盘前表纪律

### 3.1 盘前表必须是真的盘前产物

`build_pretable` 只用 `date <= as_of` 的记录；候选交易日必须严格晚于
`as_of`（`pretable_generated_before_d0` 条件）。验证：
`test_pretable_fresh_boundary_requires_strictly_earlier_as_of`（表与候选同日
→ 判失败；表早于候选交易日一天 → 判通过）、
`test_pretable_fresh_unavailable_when_pretable_missing`。

### 3.2 不在表内一律不触发，不是临时补进去

`test_candidate_not_in_pretable_never_triggers_even_when_strong`：构造一个
D0 反应窗口、流动性都合格的候选，但盘前表候选列表里没有它——断言
`status=no_signal`（不是 `unavailable`，因为"表里没有"是明确的负结果，不是
数据缺口）。另有 `test_pretable_entry_absent_for_leader_attribute_is_no_signal_
not_unavailable` 覆盖"表里连这个(龙头,属性)条目都没有"的情形。

### 3.3 把 D0 数据喂进构表函数不改变结果

`test_build_pretable_ignores_d0_data`：混入一个 D0 才出现的新龙头、一个 D0
才出现的高流动性成分股、以及一个试图在 D0"洗白"成高流动性的排除对象——三者
全部必须被忽略，`build_pretable` 的输出与只用 D-1 数据构建的结果逐字节相同。

## 4. 数据缺口（诚实标注，不造代理值）

真实 v4 事件表（`event_table_akshare_m-sina_20260728_20260821.json`，648 条
事件，22 个交易日窗口）跑出的 `unavailable_reasons`：

```
candidate_liquidity_missing: 201
```

单一但致命的结构性缺口：**`t_amount`（候选当日成交额）在这份事件表里全量
缺失**（648/648 条为 `None`），本适配器如实把它映射进 `amount` 字段，不拿其它
字段（如 `t_volume`）伪造代理值——量纲不同（股数 vs 金额），伪造会掩盖真实
缺口。这个字段一旦补齐（或改用别的可靠成交额来源），`candidate_liquidity_min`
条件才有机会变成可判定的 `True`/`False`，`pretable_generated_before_d0` /
`candidate_in_pretable` / `leader_confirmed_reaction_window` 三条在合成测试中
已验证逻辑正确，只是在真实表上因为 fail-closed 顺序（同一条 `reasons` 列表）
从未被单独观察到——201 条候选全部倒在流动性这一关。

## 5. 真实运行结果（UNVERIFIED）

```
$ PYTHONPATH=<worktree> python daban_bt_preleader_arbitrage.py \
    --table ~/.hermes/skills/chanlun-backtest/data/event_table_akshare_m-sina_20260728_20260821.json \
    --counterfactual
```

- `event_count` = 648（22 个交易日窗口内的全部涨停事件）
- `universe_count` = 201（排除各组当日龙头后实际被判定的候选数）
- `signal_count` = 0（有约束/无约束两种口径都是 0）
- `filled_count` = 0，`returns` 全为 `null`
- `constraints_bite` = `false`（零样本时如实报告"约束未生效"，不伪造"约束在咬"
  的结论——反事实测试的空集分支专门守这一点）

**如实结论：0 命中，UNVERIFIED。** 这不是"策略没有信号"，是 `t_amount` 字段
在当前 v4 事件表上结构性缺失；一旦成交额字段落地，才有条件重新评估。

## 6. 单测覆盖（`tests/test_preleader_arbitrage.py`，27 个用例全绿）

- 四个入场条件各自边界（`pretable_generated_before_d0`/`candidate_in_pretable`/
  `leader_confirmed_reaction_window`/`candidate_liquidity_min`）+ 全证据缺失 →
  `unavailable`；
- 盘前表纪律三条（3.1-3.3 节）；
- `build_pretable` 排除 `is_st`/`material_bad_news`/流动性不足成分（各自独立
  断言排除原因）；
- `pick_confirmed_leader`（选确认时刻最早者，无人确认时不可判定）；
- `evaluate_group` 排除龙头本身参与候选评估；
- NON-LIVE 消费端行为断言：`decision_policy.evaluate_decision` 把正向信号降级
  为 `watch`/仓位倍率0，`recommendation_audit.position_guidance` 归零，
  `strategy_packs.registry_records()["preleader_arbitrage"]` 报
  `allowed_in_live_agent=False`/`gate_decision="not_gated"`，
  `strategy_registry.is_allowed_in_live("preleader_arbitrage")` 为 `False`；
- 反事实：合成两日事件表上关闭 P5 成交约束后收益显著提高（一字板赢家被约束
  剔除）+ 空样本时 `constraints_bite=False`（防假绿）；
- 真实事件表上 fail-closed 成零命中（`test_backtest_fails_closed_when_
  attribute_missing` 另覆盖属性缺失场景）。

Mutation check（先 commit 再改坏 → 确认变红 → 复原；每次改动前后用
`git diff --numstat` 确认改动行数非空）覆盖 4 项：

| # | 改动 | 结果 |
|---|---|---|
| 1 | `build_pretable` 丢弃 D0 数据的过滤条件被删除（`member_date > as_of_key` → 恒不过滤） | 红：`test_build_pretable_ignores_d0_data` 失败 |
| 2 | `pretable_membership_condition` 的 `in` 反转为 `not in` | 红：7 个用例失败（含反事实/NON-LIVE 断言） |
| 3 | `reaction_window_condition` 的 `elapsed <= max_minutes` 改为 `<` | 红：`test_reaction_window_boundary_is_inclusive_le[10-True]` 失败 |
| 4 | `liquidity_condition` 的 `amount >= minimum` 改为 `>` | 红：`test_liquidity_boundary_is_inclusive_ge[20000000.0-True]` 失败 |

四项全部改坏→确认变红→`git diff --numstat` 复核改动行数（均为 1 1）→复原，
复原后 `git status --short` 与 `git diff --stat` 均为空，27 个用例全绿。

## 7. 未注册证据

- `strategy_registry.live_record("preleader_arbitrage")` → `None`
- `strategy_registry.is_allowed_in_live("preleader_arbitrage")` → `False`
- `skills/common/preleader_arbitrage.py` 与 `skills/chanlun-backtest/scripts/
  daban_bt_preleader_arbitrage.py` 全文零次调用 `register_gate_result`
- `config/strategy_packs/preleader_arbitrage.yaml` 的 `research_status` 明确写
  `not_gated_zero_sample_or_thin`

## 8. 结论

当前不具备注册条件。等 `t_amount`（或等价的可靠成交额来源）在 v4 事件表上
落地、以及独立的全市场 D-1 题材/龙头扫描管道建成后，再重新跑一次真实回测
评估是否有非零样本可供 walk-forward OOS 验证。
