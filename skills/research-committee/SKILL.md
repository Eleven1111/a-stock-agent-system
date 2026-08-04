---
name: research-committee
description: >-
  Multi-expert research plane for A-share candidates: a deterministic task
  bus enqueues bounded research tasks, Hermes/OpenClaw model turns claim
  (task, role) work orders with a shared token-bounded evidence pack, and a
  deterministic synthesis reduces the blackboard into gated proposals. No
  live state writes, no order placement.
version: 1.1.0
author: Luna
metadata:
  hermes:
    tags: [A股, 研究委员会, 多专家, 黑板, 证据包]
    category: finance
---

# 研究委员会（多专家研究平面）

三平面架构中的研究平面：事实平面（确定性 DAG）产出触发器与证据，本平面把
"解释与研究"拆成无状态专家轮次，裁决平面（decision policy / strategy
registry）保持唯一门禁。首轮专家通过黑板独立提交；发生争议时，下一轮只读取
由上一轮 finding 生成、带轮次与内容哈希的新 evidence pack，不直接读取可变黑板。

```text
research-dispatch (cron, 确定性)          ← 触发扫描：候选池 top-K、行为风险、结算亏损
  -> research_tasks.json (总线, claim/TTL)
expert_runner next (模型轮次入口)          ← claim (task, role) 工单
  -> 共享 evidence pack (内容寻址, 硬预算)
  -> 专家推理 → 独立审批 artifact → expert_runner submit
  -> 黑板集齐 → 单赢家 deterministic synthesis → verdict + 报告一次成文
  -> disputed → round-specific evidence pack → bounded escalation/adjudicator
  -> advance 时写 proposals/pending/（policy_gate_required=true）
  -> fresh PIT context + proposal/synthesis/approval binding → review execution plan
```

## 模型轮次怎么干活（Hermes / OpenClaw 通用）

```bash
python scripts/expert_runner.py next --worker <hermes|openclaw>
```

- 返回 `{"status": "idle"}` → 直接结束，不要找事做。
- 返回 `research_work_order_v1` → 按 `instructions`（角色 profile）推理
  `evidence_pack`，把 finding JSON 写入临时文件后执行工单里的
  `submit_command`。非 abstain finding 若要脱离 `review_only`，必须由模型
  可写区之外的
  `$A_STOCK_STATE_HOME/approvals/research-committee/` 提供
  `research_finding_approval_v1`，并在 submit 时传 `--approval-file`。
  `--reviewed-by` 仅为兼容参数，不能授予执行资格。校验失败（exit 2）按错误
  信息修正后重交一次；仍失败则执行 `fail` 子命令上报。
- 证据不足 → 用 `abstain_command` 弃权，弃权是合法产出。
- 循环 `next` 直到 idle 或达到本轮预算。

## 硬边界

- 专家只读 evidence pack；禁止读 ledger/portfolio 原始文件，禁止网络检索。
  **例外：`deep_researcher` 角色**——它的职责就是执行 Serenity 深研，天然需要
  网络检索与外部文件读写，见 `experts/deep_researcher.md`。
- 本平面唯一可写区：`$A_STOCK_STATE_HOME/skills/research-committee/data/`。
  **例外：`deep_researcher` 角色**额外允许写
  `$A_STOCK_STATE_HOME/skills/stock-triage/cache/deep_research/`（深研缓存，
  `skills/common/deep_research_cache.py`），这是它完成任务的必要产出。
- verdict=advance 只产生 proposal，`policy_gate_required=true`；进入实盘排序
  必须先过 strategy registry / OOS / decision policy，与本平面无捷径。
- claim、finding、approval、synthesis 与逐轮 evidence pack 均做内容/租约绑定；
  旧 claim、任意路径审批、过期或未来 PIT 上下文一律 fail closed。
- 每日字符预算写在 `config/research_committee.json`；预算耗尽任务自动顺延。

## 角色

| 角色 | profile | 职责 |
|---|---|---|
| evidence_auditor | `experts/evidence_auditor.md` | 证据链完整性、时点一致性 |
| thesis_builder | `experts/thesis_builder.md` | 题材/传导链/龙头论点 + 自带反证 |
| risk_redteam | `experts/risk_redteam.md` | 攻击论点，唯一否决权 |
| deep_researcher | `experts/deep_researcher.md` | 执行 Serenity 深研，回流深研缓存（`kind=serenity_refresh` 单角色任务） |
| fundamentals_narrator | `experts/fundamentals_narrator.md` | 只解读决策时点可见的财务快照 |
| risk_aggressive | `experts/risk_aggressive.md` | 上行空间与错失成本，无否决权 |
| risk_neutral | `experts/risk_neutral.md` | 平衡风险收益，无否决权 |
| adjudicator | `experts/adjudicator.md` | 仅在合法最终升级轮裁决，证据不足时否决 |

## Serenity 深研并入研究平面（§6）

`serenity_refresh` 是普通的研究任务类型，走同一条总线/租约/预算：

```text
serenity-refresh-plan (cron, 确定性)      ← 复用 serenity_refresh_queue 的
  -> research_bus.enqueue_task(            due 判定(collect_targets/plan_refreshes)
       kind=serenity_refresh)
候选深研工单构建证据包时发现 deep_research 缺失/过期
  -> 同样 enqueue_task(kind=serenity_refresh)（需求牵引，见 evidence_pack.py）
expert_runner next --role deep_researcher  ← claim 工单
  -> 执行 Serenity skill → 写 deep_research_cache
  -> submit finding（fail-closed：没有 asof >= 任务交易日的新鲜缓存条目，
     submit 会被拒收，语义与旧 serenity_refresh_queue.complete_request 一致）
```

旧的独立队列 `serenity_refresh_queue.py` 的 `plan_and_save`/`claim_next`/
`complete_request`/`fail_request` 已弃用（仅用于排空历史积压），新的调度入口
是 `serenity_refresh_queue.plan_bus_refreshes`。

研究窗口可用 `--role` 只认领指定角色（如 `expert_runner next --worker
openclaw --role risk_redteam`），便于把不同角色分到不同的会话窗口；任一窗口
宕掉时，其他窗口仍可不带 `--role` 兜底认领，租约保证同一角色不双跑。

## 人工操作

```bash
python scripts/research_dispatch.py --kind user_request --code 600519 --reason "复核龙头地位"
python scripts/research_dispatch.py --kind deep_debate --code 600519 --reason "基本面与风险三方复核"
python scripts/expert_runner.py status            # 队列与角色状态
python scripts/expert_runner.py synthesize        # 手动合成 ready 任务
```

## PIT、执行计划与校准入口

这些入口故意不伪装成“自动交易流水线”：

- `research-dispatch` 已注册在 `cron/hermes-cron-manifest.json`，只负责从真实
  DAG 事实确定性入队；`deep_debate` 在通过样本外验收前保持显式触发。
- `scripts/fundamentals_snapshot.py` 只接收 provider adapter 已提供的
  `event_time/published_at/available_at/captured_at/watermark/sealed_at`。仓库没有
  可证明的财报 provider 时，不得由 cron 合成这些时间。
- `scripts/compile_research_execution_plan.py` 必须同时消费已绑定的 synthesis
  或受信 proposal approval，以及 fresh market/portfolio/quality/strategy PIT
  context；输出始终 `execution_eligible=false`，没有 broker/order 副作用。
- `scripts/expert_calibration.py` 只接收唯一
  `(task_id, code, decision_date)`、最终结算、严格 OOS dataset/batch lineage；
  结果只进入人工复核队列，不自动修改策略或专家权重。
