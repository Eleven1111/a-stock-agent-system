# 前向标签的三种含义

同一列「收益率」在本仓库里可能是三个不同的测量。它们单位相同、在报告里长得一样，
所以必须有各自的名字和各自的门。

| `label_kind` | 测的是什么 | 入场 | 出场 | 成交模型 | 可作执行准入证据 |
|---|---|---|---|---|---|
| `price_path_prediction` | 价格路径动了没有 | 次一交易日**参考开盘价** | 第 `horizon` 个交易日收盘 | 无 | **否** |
| `executable_simulated_result` | 这笔仓位能不能建、能不能平 | 次一交易日开盘，**过成交约束** | 自**实际成交日**起 T+1 之后的收盘，过卖出约束 | 有 | 是 |
| `manual_recorded_fill` | 人工从券商对账单录入的真实成交 | 实际 | 实际 | — | 是 |

## 为什么必须分开

`strategy_forward_settlement` 的主 horizon 对六个策略里的四个是 **1**。horizon=1 的标签是
「次日开盘买、**当日**收盘卖」——A 股现金账户做不到。这个标签本身没有错，它是一个
合法的价格路径测量；错的是拿它去回答「这个策略能不能交易」。

产物一直带着 `research_only: True` / `execution_eligible: False`，但**没有一个消费端在检查它**。
现在补上：

```python
from forward_label_taxonomy import assert_execution_evidence
assert_execution_evidence(dataset)   # price_path 标签在这里抛 LabelKindError
```

问「价格动了没有」的研究门**不该**调用它；问「能不能交易 / 能不能晋级执行」的门必须调用。

## `net_forward_return` 是什么

是**假设成本调整后的标签**，不是实际可成交净收益：它按 `cost_model` 里的固定滑点
和假设名义本金调整，没有过任何成交约束。`research_clock.cost_basis` 字段写成
`modelled_assumption` 就是为了让它在报告里无法被读成实际成交结果。

## 可执行路径

`skills/common/executable_forward_simulation.py`，同一个冻结信号：

- 入场过 `execution_constraints.assess_buy_fill`：一字涨停 → `not_filled`（**保留在分母里**，
  不从成功子集剔除）；回封按参与率部分成交；数据缺失 fail-closed。
- **T+1 从实际成交那一天起算**，`hold_sessions < 1` 直接抛异常。
  「相对决策日的 T+1」和「持有一天」是两件事。
- 出场过 `assess_sell_fill`：跌停无承接 → 顺延到下一个可成交日并计 `days_blocked`；
  一直到数据尽头仍不能卖 → `unresolved_right_censored`，不静默丢弃。
- 信号产生时刻晚于入场时刻 → `pending_evidence`，不回填成交。
- 费用与滑点全部复用 `execution_model.net_return_pct`，**不另写一套规则**。

同一输入重跑结果逐字段相同。

## 刻意没做

- 没改 `config/strategy_forward_settlement.json`：`approved_policy_hashes` 与
  `approved_strategy_rule_hashes` 一动，历史批准全部失效。标签语义没变，只是把
  一直隐含的时钟写了出来。
- 没有回改任何历史结算产物。`label_kind` / `research_clock` 是**数据集级**的新增字段，
  行契约（`config/dataset_catalog.json` 的 `settled_forward_samples_v1`）未动。
- 盘中原策略（竞价即时入场、分歧回封）仍是 `unvalidated_intraday`：
  本模块只检验「隔日延续」，缺分钟级 PIT 与可成交证据，不冒充验证了原策略。
