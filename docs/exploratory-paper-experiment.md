# 探索性 paper 实验：三道不同的门

## 「允许实验」不是「认可策略」

| 阶段 | 需要的证据 | **不**作为前置条件 |
|---|---|---|
| 预测数据积累 | 冻结定义、真实可用时点、数据质量、清晰标签 | 已证明策略赚钱 |
| **探索性模拟实验** | 冻结实验、独立账户 scope、可成交/T+1 纪律、输入契约、预算 | 券商对账、完整 OOS |
| 真实决策准入 / 受认可 pilot | 现有研究门 + 审批 + 验证 + **真实对账协议** | — |

**`strategy_registry` 的 `paper_only` 晋级通道一行未动**，broker 对账门原样保留。
为了让 paper 动起来去删真实晋级里的 broker 门，是把第三档偷换成第二档。

## 消费断点：晋级过但没人在跑

`strategy_registry` 早就有 `paper_runtime_allowed()` / `paper_live_weight()`，
但 `grep -rn "paper_runtime_allowed" skills/paper-trading/` **零命中**——
`paper_trading_runner.py` 从未 import 过 `strategy_registry`，它只看
`config.runtime.mode == "paper_only"`。也就是说「已晋级 ⇒ 已运行」这句承诺只存在于文档里。

现在两条入口都必须显式声明：

- `entry_point: "pilot_permission"` —— **真的去读**注册表的
  `paper_runtime_allowed` + `paper_live_weight`；权限为假或权重为 0 一律不准入。
- `entry_point: "exploratory_scope"` —— **不使用** pilot 权限，走独立 scope，
  `claims_research_gate_passed: false`、`research_gate_passed: false`。

## 权重语义

`paper_pilot_weight` 受 `maximum_manual_pilot_weight`（0.1）封顶，
是**组合预算比例**（`WEIGHT_SEMANTICS = "portfolio_budget_fraction"`），
不是单票仓位比例。单票另有 `budget.max_position_fraction`，两者都受现金与集中度上限约束。

## 默认实验

`config/exploratory_paper_experiments.json` → `rank_surprise_next_open_paper_v1`。

**明确是 S1 的隔日延续变体**：D 日收盘后冻结信号，次交易日开盘买，持有 1 个交易日、
自**实际成交日**起 T+1 之后收盘卖。**不检验、也不声称检验了**原策略的竞价即时入场——
那条路径缺分钟级 PIT 与可成交证据，仍是 `unvalidated_intraday`。

冻结绑定：`experiment_id` / `strategy_id` / `strategy_rules_sha256` / `sample_start` /
时钟三件套 / `account_scope` / `budget` / `ranking`，一起进 `experiment_sha256`。
改任何一项 → hash 不匹配 → `admit` 直接拒绝。**看过结果再换实验是不允许的**：
改选必须先改冻结记录，改动本身留痕。

## 金线

```
冻结实验 → admit（scope/权限）→ rank_candidates（事前排序）
        → select_within_budget（预算，被挤掉的记 rejected 不丢）
        → simulate_admitted（executable_forward_simulation：成交约束 + 真 T+1）
        → summarise_run（成交/未成交/未解决全部留在分母里）
```

- **排序事前固定**，不依赖上游返回顺序；最终 tie-break 是 `entity_id`，
  一个事前注册、不含任何结果信息的键。并列不按后验收益挑。
- **幂等键带 scope**：`scope_idempotency_key` 拼进 `account_scope` + `experiment_id` +
  `experiment_sha256[:16]`。原来的 `paper.candidate_evaluated:{asof}:{code}` 不带 scope，
  两个实验同日同票会互相吞掉事件。
- 零候选 → `no_eligible_evidence`，**不是**一次空的成功；
  也不自动改跑表现更好的另一个策略。
- 重跑同一金线 `run_sha256` 相同，不多一笔成交。

## 硬边界

不改真实持仓、不接 broker、不发真实订单、不降低真实晋级标准。
每条产物带 `research_only: true` / `live_order_sent: false`。
