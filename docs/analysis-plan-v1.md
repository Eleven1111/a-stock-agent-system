# Analysis Plan v1

`analysis_plan_v1` 是一个受限的研究问题执行层，不是通用代码生成器。它把已经
通过 `dataset_contract_v1` 的输入连接成确定性 DAG，并把每个节点的输入/输出
hash 写进 `analysis_run_v1.lineage`。

## 当前白名单

| operator | 输入 | 输出 | 参数 | 用途 |
|---|---|---|---|---|
| `group_direction_cohorts_v1` | `cross_sectional_direction_rows_v1` | `direction_cohorts_v1` | 无 | 按 src/dst 组成方向检验队列 |
| `group_settled_outcomes_v1` | `settled_signal_outcomes_v1` | `direction_cohorts_v1` | `strategy_id`(必填)、`return_basis`(gross/net，默认 net) | 按策略切出该策略自己的队列，可选税前/税后口径 |
| `cross_sectional_direction_v1` | `direction_cohorts_v1` | `cross_sectional_direction_v1` | 无 | Rank IC、分位差、独立样本和方向判定 |
| `discovery_recall_v1` | `discovery_recall_input_v1` | `discovery_recall_report_v1` | 无 | D0、竞价、可执行与开盘阶段召回损失 |

没有任意 Python、模块名、函数路径或动态 import。新增算子仍需扩充 operator
contract 与测试，不能从计划文件注入。

## 参数（自 operator-params 起）

节点 `params` 不再必须为空，但仍是**白名单**：每个算子在 `OPERATORS` 里声明
`param_schema`，未声明的参数名一律拒绝，类型、枚举与数值上下界在计划编译期
（`seal_plan`）就校验，运行期不再兜底。

**参数是选择器，不是判定阈值。** 判定阈值（如 `cross_sectional_direction` 的
`min_pairs_per_cohort`）在其模块里写明「固定为常量：调参 = 重新过闸」，因此
不暴露为参数——否则计划文件会成为绕开研究闸门的旁路。可以参数化的是"看哪部分
数据"（选哪个策略、用税前还是税后口径），不是"判多严"。

参数写在节点正文里，因此自动计入 `plan_hash`：改参数必然改哈希，"换了参数
还声称是同一个计划"这条路走不通。`analysis_run_v1.lineage` 每个节点另记
`operator_version` 与解析后的 `params`（含默认值），使重放可复查。

## 计划结构

```json
{
  "schema": "analysis_plan_v1",
  "plan_id": "direction-and-recall",
  "question": "排序方向是否成立，候选在哪一层丢失？",
  "research_only": true,
  "inputs": {
    "direction_rows": {
      "kind": "dataset",
      "dataset_id": "cross_sectional_direction_rows_v1",
      "contract_hash": "<当前 contract hash>",
      "catalog_hash": "<当前 catalog hash>",
      "coverage_ratio": 0.98
    },
    "recall_snapshot": {
      "kind": "inline",
      "schema": "discovery_recall_input_v1"
    }
  },
  "nodes": [
    {
      "id": "cohorts",
      "operator": "group_direction_cohorts_v1",
      "inputs": ["direction_rows"],
      "params": {}
    },
    {
      "id": "direction",
      "operator": "cross_sectional_direction_v1",
      "inputs": ["cohorts"],
      "params": {}
    },
    {
      "id": "recall",
      "operator": "discovery_recall_v1",
      "inputs": ["recall_snapshot"],
      "params": {}
    }
  ],
  "outputs": ["direction", "recall"]
}
```

## 执行

```bash
python scripts/run_analysis_plan.py \
  --plan /path/to/plan.json \
  --inputs /path/to/inputs.json
```

输入、计划、catalog 与 engine version 共同生成 cache key。缓存产物还带独立的
`result_hash`；文件内容被篡改时不会命中，而会重新执行白名单 DAG。

## 失败关闭

下列情况在任何算子运行前 blocked：

- 未知算子、未知字段、非空 params 或任意代码/module 字段；
- DAG 循环、缺少依赖或节点输入 schema 不匹配；
- catalog、contract 或 plan hash 不匹配；
- 数据记录、PIT 或覆盖率没有通过 dataset contract；
- 漏斗输入没有固定 `generated_at`，或可选阶段不是数组/null；
- 执行输入缺失或出现计划外输入。

## 边界

- 输出固定 `research_only=true`、`trading_action=none`。
- `direction_confirmed` 仍不等于策略获准进入实时排名。
- 召回报告始终 `execution_gate_unchanged=true`，不能自动扩大候选池。
- 新算子必须复用确定性模块并单独评审，不能由模型临时注册。
