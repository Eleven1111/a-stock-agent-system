---
name: research-committee
description: >-
  Multi-expert research plane for A-share candidates: a deterministic task
  bus enqueues bounded research tasks, Hermes/OpenClaw model turns claim
  (task, role) work orders with a shared token-bounded evidence pack, and a
  deterministic synthesis reduces the blackboard into gated proposals. No
  live state writes, no order placement.
version: 1.0.0
author: Luna
metadata:
  hermes:
    tags: [A股, 研究委员会, 多专家, 黑板, 证据包]
    category: finance
---

# 研究委员会（多专家研究平面）

三平面架构中的研究平面：事实平面（确定性 DAG）产出触发器与证据，本平面把
"解释与研究"拆成无状态专家轮次，裁决平面（decision policy / strategy
registry）保持唯一门禁。专家之间不对话，只通过黑板汇总。

```text
research-dispatch (cron, 确定性)          ← 触发扫描：候选池 top-K、行为风险、结算亏损
  -> research_tasks.json (总线, claim/TTL)
expert_runner next (模型轮次入口)          ← claim (task, role) 工单
  -> 共享 evidence pack (内容寻址, 硬预算)
  -> 专家推理 → expert_runner submit (schema 校验 finding)
  -> 黑板集齐 → 确定性 synthesis → verdict + 报告一次成文
  -> advance 时写 proposals/pending/（policy_gate_required=true）
```

## 模型轮次怎么干活（Hermes / OpenClaw 通用）

```bash
python scripts/expert_runner.py next --worker <hermes|openclaw>
```

- 返回 `{"status": "idle"}` → 直接结束，不要找事做。
- 返回 `research_work_order_v1` → 按 `instructions`（角色 profile）推理
  `evidence_pack`，把 finding JSON 写入临时文件后执行工单里的
  `submit_command`。校验失败（exit 2）按错误信息修正后重交一次；仍失败则
  执行 `fail` 子命令上报。
- 证据不足 → 用 `abstain_command` 弃权，弃权是合法产出。
- 循环 `next` 直到 idle 或达到本轮预算。

## 硬边界

- 专家只读 evidence pack；禁止读 ledger/portfolio 原始文件，禁止网络检索。
- 本平面唯一可写区：`$A_STOCK_STATE_HOME/skills/research-committee/data/`。
- verdict=advance 只产生 proposal，`policy_gate_required=true`；进入实盘排序
  必须先过 strategy registry / OOS / decision policy，与本平面无捷径。
- 每日字符预算写在 `config/research_committee.json`；预算耗尽任务自动顺延。

## 角色

| 角色 | profile | 职责 |
|---|---|---|
| evidence_auditor | `experts/evidence_auditor.md` | 证据链完整性、时点一致性 |
| thesis_builder | `experts/thesis_builder.md` | 题材/传导链/龙头论点 + 自带反证 |
| risk_redteam | `experts/risk_redteam.md` | 攻击论点，唯一否决权 |

研究窗口可用 `--role` 只认领指定角色（如 `expert_runner next --worker
openclaw --role risk_redteam`），便于把不同角色分到不同的会话窗口；任一窗口
宕掉时，其他窗口仍可不带 `--role` 兜底认领，租约保证同一角色不双跑。

## 人工操作

```bash
python scripts/research_dispatch.py --kind user_request --code 600519 --reason "复核龙头地位"
python scripts/expert_runner.py status            # 队列与角色状态
python scripts/expert_runner.py synthesize        # 手动合成 ready 任务
```
