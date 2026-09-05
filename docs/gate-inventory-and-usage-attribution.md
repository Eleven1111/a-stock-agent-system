# 门禁清单、模型评测与成本关联

## 门禁分三类——先出清单，默认不改生产规则

| 类别 | 例子 | 处置 |
|---|---|---|
| **硬执行约束** | T+1、现金、真实可成交性、权限、时点、必需数据完整性 | **所有对照臂一致保留**，消融它测不出任何真东西 |
| **待证实策略过滤** | MFI、缠论、经验评分阈值、状态过滤 | 仅在独立实验内消融 |
| **解释性证据** | 没有实际影响排序/决策的记录 | 记录用途；**不算风险控制贡献** |

`skills/common/gate_inventory.py` 只**建清单和记账**。`production_rules_changed: false`
写在产物里——哪些过滤该留是后面基于证据的研究决定，不是本轮顺手改掉的东西。
一个标成「解释性」却真的影响排序的门会被标 `misfiled_explanatory_gates`。

## 每道待证实门必须能回答的七个数

`REQUIRED_COUNTS`：门前候选数 / 规则拦截 / 数据缺失 / 迟到 / 执行拒绝 /
已终结结果 / 未解决。**进去的每一个候选都得从某处出来**：
`denominator_balanced` 不成立就返回 `not_evaluated`，不给结论。

`REQUIRED_DELTAS`：同口径净收益 / 回撤 / 换手 / 资金暴露。缺任何一项 → `not_evaluated`。

拒绝样本不从分母消失。但「被拒后涨了」也**不自动等于错杀**：
产物里带 `miss_requires: [was_fillable, capital_available, holding_period_matched]`——
当时买不买得到、资金是否已用尽、持有周期是否对得上，三个都要回答。

## `fact_plane_writes` 曾经是硬编码的 0

`scripts/evaluate_agent_harness.py` 第 159 行写死 `"fact_plane_writes": 0`。
读起来像实测结果，其实什么都没测。改成真的去数之后：

```
"fact_plane_writes": {
  "attempts_declared": 8,       ← 这套用例里其实有 8 次声明写事实面
  "blocked_attempts": 8,
  "completed_writes": 0,
  "guarantee_scope": "static_protocol_only",
  "measured_against": "frozen_turn_fixtures",
  "not_evidence_of": "operating_system_level_write_isolation"
}
```

冻结用例证明的是**契约挡住了被声明的写**。进程到底有没有权限写那些路径，
是一个操作系统层的问题，这套 harness 从来没问过——报告现在直说这一点，
不再让人以为做过权限实验。

## 真实宿主评测

`scripts/evaluate_openclaw_host.py` + `evals/openclaw_host/cases.json`（20 个分层任务）：
正常证据 4 / 矛盾来源 3 / 信息不足 3 / 陈旧或未来材料 2 / 工具失败 2 /
长上下文 2 / 角色误路由 2 / 报告中夹带指令 2。

**20 例是工程验收建议，不是金融有效性的样本门槛。** 产物里写死
`scope: engineering_integration_only` 和 `not_a_claim_about: [strategy_validity,
investment_performance]`。

指标刻意把常被混为一谈的东西分开：

- **引用可解析** vs **引用真正支持结论**（`citation_support_gap`）
- **技术失败** vs **合理弃答** vs **无依据弃答**（`abstention_split`）
- **提供了研究判断** vs **真正改善了预注册的独立评分**
  （`judgement_without_score_improvement`）

本机**没有安装 openclaw**，所以状态是 `not_run / openclaw_binary_not_found`。
**不拿 fake 模型顶替后声称通过**——一个没跑的评测报告自己没跑。

## 成本关联：只补薄薄一层

`skills/common/usage_attribution.py`。**不新增计费平台**，只把宿主已有的
session/usage 记录和仓库已有的 task/role/run 绑起来。

两条硬规矩：

- **缺价格是 `unknown`，不是 0。** 零是一个测量结果，缺失不是。
  采纳结果为 0 时每采纳成本返回 `"undefined"` 而不是 0。
- **一条宿主 run 只计一次。** 父 turn 与其子任务都上报时，
  子记录折进父记录（`folded_into_parent`），不叠加双算。

确定性 command 作业没有模型调用 → 模型 token 记 0（`no_model_call_in_this_job`），
但 CPU / 取数 / IO 成本另列为 `unknown`。

## 裁剪原则

`retirement_decision: "requires_dependency_closure_and_owner_review"`。
「30 天未影响排序」**不自动停任务**：事故诊断、低频风险事件、证据预热、
审计保留都是有效用途。依赖闭包、运行成本、真实消费、禁用影响四项都核实过，
才在授权范围内停用明确冗余的工作。
