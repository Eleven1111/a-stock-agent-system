# 多专家研究平面（Research Committee）工作日志

日期：2026-07-03 ｜ 里程碑：M1（最小闭环）已完成并全量验证

---

## 1. 背景与决策记录

### 1.1 需求

把 a-stock-agent-system 升级为多专家 agent 模式，让部署机上的 Hermes 与
OpenClaw 按各自长板协同运行。

### 1.2 对 Codex 升级方案的审核结论（已否决，仅采纳原则层）

外部 Codex 方案（`a-stock-hermes-openclaw-upgrade-plan.zh.md`）审核结果：

- 其 Phase 1–6 拟"新增"的 manifest、DAG runner、隔离 job runner、快照、状态
  投影、validator **在仓库中全部已存在且更成熟**，照做会产生一套不兼容的平行
  协议（`depends_on` vs 现网 `context_from`、`--manifest` CLI vs 位置参数）。
- "选一个 canonical 副本、废掉另一个"倒退于现有 `A_STOCK_STATE_HOME` 单一状态
  根 + `run_lease` 双活互斥架构，并把 `state/` 画进仓库目录（与状态外置冲突）。
- 新造 `A_STOCK_AGENT_SYSTEM_ROOT` 环境变量与现有 `A_STOCK_STATE_HOME` 形成第
  二套事实源定位机制。
- Phase 7 串行 bridge 会误竞价窗口时点；回滚方案 `mv ~/.hermes` 会连 signal
  ledger 一起回滚（吞账本）。
- 用户真正要的多专家层只有 8 个角色名，无通信契约、无 token 预算、无调度模
  型、无失败语义。

**采纳**：其 §2（不做什么）、§4.3（Decision Policy 唯一裁决）、§10（门禁表）
与本仓库 AGENTS.md 契约一致，作为研究平面的边界条款沿用。**其余废弃。**

### 1.3 采用方案

"三平面 + 黑板 + 证据包"：事实平面与裁决平面零改动，只新建研究平面。

---

## 2. 方案完整版本

### 2.1 设计原则（第一性）

1. **事实由确定性代码产出，模型轮次只做解释。** 专家不抓数据、不算指标，只读
   证据包。
2. **专家无状态、证据有界、触发驱动。** 无触发即无任务，静默 = 零 token。
3. **专家之间不对话。** 自由对话是 token 的 N² 黑洞；专家单轮写黑板，确定性
   reducer 合成，分歧显式输出（分歧本身是信号，不由模型"和稀泥"）。
4. **角色绑定是配置，不是架构。** 任何运行时都能 claim 任何角色，租约保证不
   双跑；分工调整改配置即可，天然互为热备。
5. **研究平面对事实平面只读。** 唯一可写区是自己的 skill data 目录；方向性产
   物只能是 proposal，`policy_gate_required=true` 永远为真。

### 2.2 三平面架构

```text
┌─ 事实平面（现有，零改动）──────────────────────────────────┐
│ 40-job DAG · 不可变快照 · signal ledger · T+1/T+3 结算       │
│ lite 状态投影 · run lease · dual-runtime audit               │
└───────────────┬────────────────────────────────────────────┘
                │ 触发器（确定性扫描：候选池 top-K / 行为风险 / 结算亏损 / 人工）
                v
┌─ 研究平面（本次新建）──────────────────────────────────────┐
│ research-dispatch (cron 命令 job，无模型轮)                  │
│   -> Research Bus: research_tasks.json（claim/TTL/预算）     │
│ expert_runner next  ←—— Hermes / OpenClaw 模型轮次入口       │
│   -> 共享 Evidence Pack（内容寻址、硬预算、fail-closed）     │
│   -> 专家推理 -> expert_runner submit（schema 校验 finding） │
│   -> 黑板集齐 -> 确定性 Synthesis -> verdict + 报告一次成文  │
│   -> verdict=advance 时写 proposals/pending/                 │
└───────────────┬────────────────────────────────────────────┘
                │ 仅 proposal / report，永不直写事实
                v
┌─ 裁决平面（现有，零改动）──────────────────────────────────┐
│ decision_policy · strategy_registry / OOS 研究门控 · 审计    │
└────────────────────────────────────────────────────────────┘
```

### 2.3 组件设计

#### Research Bus（`skills/common/research_bus.py`）

泛化仓内已验证的 `serenity_refresh_queue` claim 模式：

- 任务状态机：`pending → in_progress → ready_to_synthesize →
  done | failed | abstained | rejected`；角色粒度状态机：
  `pending → claimed → done | failed`。
- **claim 粒度是 (task, role)**：同一任务的三个专家可被两个运行时并行认领。
- claim 带 TTL（默认 120 分钟），崩溃后租约过期自动回收重派；每角色最多
  2 次尝试，超限任务 failed，不静默丢失。
- 入队去重：同 (kind, subject) 活跃任务不重复入队；终态任务在 cooldown
  天数内不重复研究（`force` 可越过）。
- 每日预算账本（`budget/{date}.json`）：claim 时按任务预估字符数预留，超出
  当日预算的任务留在队列顺延（`deferred_reason=daily_budget_exhausted`），
  不丢弃；submit 时回写实际字符数，供预算校准。

#### Evidence Pack（`skills/common/evidence_pack.py`）

token 经济的核心杠杆，每任务构建一次、全部专家共享：

- 组装内容（全部来自已存在的事实）：agent state 的 **subject 切片**（主体相
  关持仓/推荐/信号 + 计数，非全量 dump）、按任务类型配置的 cron artifact
  `summary` + 有界 `stdout_excerpt`、候选池条目、深研缓存摘要。
- **硬字符预算**（按任务类型配置，deep_dive 24000 字符），超预算按确定性序列
  削减：丢 artifact 摘录 → 丢 subject_data → 截前 3 个 artifact → 压缩
  agent_state → 折叠 artifact 为状态行；削减动作记录在 `reductions`。
- **内容寻址**：payload 的 sha256 即 pack ref，相同输入命中缓存不重建。
- **质量分级 fail-closed**：必需 section 缺失 → `insufficient`（专家轮次直接
  自动弃权，**零 token**）；artifact 缺失/过期 → `degraded`（进入包内标记，
  由证据审计专家显式处理）。

#### 通用 Expert Runner（`scripts/expert_runner.py`）

一套基建 × N 个角色 profile，不是 N 套基建：

- `next --worker <runtime>`：认领工单，输出
  `research_work_order_v1`（角色指令 + 共享证据包 + 输出契约 + 提交命令）；
  队列空输出 `{"status":"idle"}`。
- `submit --task --role --file`：finding 过 schema 校验（stance 枚举、
  confidence ∈ [0,1]、`support` 必须带非空反证与失效条件、`abstain` 必须给
  理由、总长限制）；校验失败 exit 2 附错误清单，修正后可重交；末位角色提交
  自动触发确定性合成。
- `abstain` / `fail` / `status` / `synthesize`：弃权、失败上报、状态查询、
  手动合成兜底。
- 证据包 `insufficient` 时 runner 直接替专家弃权，模型不消耗任何 token。

#### 黑板 + 确定性 Synthesis（`skills/common/research_synthesis.py`）

- 专家 finding 落 `board/{task_id}/{role}.json`，互相不可见、不对话。
- verdict 规则（纯代码，无模型轮）：

| 条件（按优先级） | verdict | 效果 |
|---|---|---|
| 全员 abstain | `abstained` | 终态，无产物 |
| risk_redteam oppose 且 confidence ≥ 0.7 | `rejected` | **一票否决**，终态 |
| support ≥ 0.6 且 oppose ≥ 0.6 同时存在 | `disputed` | 分歧显式输出（或有界升级） |
| 存在 support ≥ 0.6 且无高置信 oppose | `advance` | 写 proposal（仍需过全部门禁） |
| 其余 | `watch` | 报告留档，无 proposal |

- **有界升级**：`disputed` 且配置开启时，冲突双方角色重开一轮（附对方最强论
  据），硬上限 `max_rounds`（默认 1，M1 默认关闭）；再冲突即终态 disputed。
- 报告 markdown **一次成文**（不让每个专家各写散文）；`advance` 额外落
  `proposals/pending/{task_id}.json`，带 `policy_gate_required: true` 与
  `live_effect: none_until_strategy_registry_and_decision_policy_pass`。
- 任务生命周期事件追加 `research_ledger.jsonl`（append-only，独立于 signal
  ledger，不与事实账本竞争）。

#### 触发器（`scripts/research_dispatch.py` + manifest job `research-dispatch`）

确定性命令 job（15:50 交易日，闭市后），无模型轮：

| 触发器 | 来源事实 | 任务类型 | 默认专家计划 |
|---|---|---|---|
| 候选池 top-K（K=2） | `candidate_pool_latest.json` | candidate_deep_dive | auditor + thesis + redteam |
| 行为风险 level ∈ {high, critical} | agent state `behavior_risk` | anomaly_review | redteam |
| T+3 终结算亏损 ≤ -5%（每日最多 1 个） | agent state `signals` | postmortem | auditor + redteam |
| 人工 `--kind user_request --code/--theme` | 用户请求 | user_request | 三专家 |

### 2.4 Schema 一览

| schema | 位置 | 说明 |
|---|---|---|
| `research_task_v1` | `research_tasks.json` | 任务 + 角色状态机 + 预算 |
| `research_evidence_pack_v1` | `packs/{sha256}.json` | 内容寻址证据包 |
| `research_work_order_v1` | expert_runner stdout | 模型轮次唯一输入面 |
| `research_finding_v1` | `board/{task}/{role}.json` | 专家结论（校验后落盘） |
| `research_synthesis_v1` | `board/{task}/synthesis.json` | 确定性合成结果 |
| `research_proposal_v1` | `proposals/pending/` | 门禁前研究提案 |
| `research_budget_v1` | `budget/{date}.json` | 每日预算账本 |
| `research_dispatch_v1` | dispatch stdout/artifact | 触发扫描摘要 |

所有运行数据都在 `$A_STOCK_STATE_HOME/skills/research-committee/data/` 下，
不进 git；schema 版本号内嵌，后续演进走 v2 并存。

### 2.5 Token 经济机制（与 Codex Level 3 对照）

| 维度 | Codex Level 3 | 本方案 |
|---|---|---|
| 每任务模型轮次 | 8 角色 + orchestrator ≥ 9 轮 | 1–3 轮（+至多一轮有界升级） |
| 每轮输入 | 全量 state/artifacts/ledger | 共享证据包，硬预算 16K–24K 字符 |
| 专家间通信 | 未定义（隐含对话） | 零对话，黑板单轮 |
| 编排者 | 模型轮 | 确定性代码（bus + reducer） |
| 报告成文 | 每专家各写 | synthesis 一次 |
| 空闲成本 | 未定义 | 触发驱动，静默 = 0 |
| 预算控制 | 无 | 每日字符预算 + 任务预留 + 实际回写 |
| 证据不足 | 未定义 | pack insufficient → 自动弃权，0 token |

实测基线（2026-07-03 演练，真实仓库配置）：

- 单角色工单（指令 + 证据包 + 契约）：**4055 字符 ≈ 1.3K token**（演练数据
  规模；真实 artifact 更满时上限锁死在 pack 预算 24K 字符 ≈ 8K token）。
- 三专家 user_request 任务预算预留：47000 字符（≈ 15K token 量级上限）。
- 默认每日预算 400000 字符 ≈ 每天约 4–8 个深研任务，超出自动顺延。

### 2.6 鲁棒性机制清单

- 任务/角色双层状态机，claim TTL 过期自动回收（崩溃安全）。
- (task, role) 租约走 `state_store.mutate_json` 单锁事务，与仓内既有并发
  模式一致；双运行时并行 claim 不同角色互不干扰，同角色不可重复认领。
- finding 强 schema 校验：support 无反证/无失效条件直接拒收；两次失败角色
  falied、任务 failed，绝不部分写入。
- fail-closed 三连：证据包 insufficient → 自动弃权；agent state 缺失 →
  required section 触发 insufficient；预算耗尽 → 顺延不丢弃。
- 研究平面唯一可写区由 manifest `allowed_state_writes` 声明并受审计。
- 每日预算账本记录预估与实际，偏差可回归校准。
- 全部模块纯标准库、cron-safe，无新增依赖。

### 2.7 与现有系统的边界

- 事实平面：仅新增 1 个 manifest job（`research-dispatch`，40/40 校验通过），
  依赖 `closing-triage`（硬）与 `candidate-discovery`（可选），复用交易日
  gate、依赖门禁、artifact、lease 全套既有机制。
- 裁决平面：零改动。proposal 进入实盘排序仍必须过 strategy_registry / OOS /
  decision_policy，与研究平面无捷径。
- AGENTS.md Core ownership 表新增研究平面一行。

---

## 3. 实现清单

### 新增文件

| 文件 | 行为 |
|---|---|
| `skills/common/research_bus.py` | 任务总线：入队/去重/冷却、(task,role) claim + TTL、finding 校验、预算账本、研究账本 |
| `skills/common/evidence_pack.py` | 证据包：subject 切片、artifact 摘要、确定性削减、内容寻址、质量分级 |
| `skills/common/research_synthesis.py` | 确定性合成：verdict 规则、否决权、有界升级、报告一次成文、proposal 落盘 |
| `scripts/expert_runner.py` | 模型轮次入口：next/submit/abstain/fail/status/synthesize |
| `scripts/research_dispatch.py` | 确定性触发扫描 + 人工入队 CLI |
| `config/research_committee.json` | 任务类型→专家计划→预算→阈值（全部集中配置，零硬编码） |
| `skills/research-committee/SKILL.md` | 运行时渐进披露入口（模型轮次操作手册） |
| `skills/research-committee/experts/{evidence_auditor,thesis_builder,risk_redteam}.md` | 三个角色 profile（统一 stance 语义 + 硬边界） |
| `tests/test_research_bus.py` 等 5 个测试文件 | 38 个新测试覆盖全部状态机路径 |

### 修改文件

| 文件 | 改动 |
|---|---|
| `cron/hermes-cron-manifest.json` | +`research-dispatch` job（15:50 交易日），39→40 |
| `AGENTS.md` | Core ownership 表 +1 行（研究平面归属） |
| `tests/test_dual_runtime_audit.py` | ruff 清理存量未使用 import（一行，无行为变化） |

## 4. 验证记录（2026-07-03 全新运行）

```text
pytest 全仓             911 passed（含 38 个研究平面新测试）
ruff check .            All checks passed
validate_cron_manifest  OK: 40 jobs (0 local, 40 external)
compileall（5 个新文件） OK
scripts/smoke_test.py   13 passed, 0 failed
```

端到端演练（独立 scratch 状态根 + 真实仓库配置，演练数据）：

1. `research_dispatch --kind user_request --code 601127` → 任务入队；
2. openclaw `next` 认领 evidence_auditor，工单 4055 字符，pack quality ok；
3. openclaw `submit`（support 0.7，带反证）→ 角色 done；
4. hermes `next` 接力认领 thesis_builder、risk_redteam（双运行时协作；已
   认领角色不可重复 claim，验证租约语义）；
5. 末位 submit → 自动合成：verdict `advance`、`policy_gate_required=true`、
   proposal + 报告落盘、research ledger 2 条事件、预算账本预留 47000/400000
   字符且回写实际 finding 字符数（398/395/298）；
6. 队列终态 `{'done': 1}`，`next` → `idle`（空闲静默）。

另验证：证据包 insufficient 时 `next` 自动弃权（0 模型 token）、finding 缺
反证被拒收（exit 2）、角色两次失败后任务 failed、租约 TTL 过期回收。

---

## 5. 安装使用说明（部署机：Hermes + OpenClaw）

### 5.1 前置（两个运行时共用）

```bash
# 1) 两个项目副本都同步到本版本（或单副本双注册）
cd <项目副本> && git pull

# 2) 确认两个运行时指向同一个状态根（这是全部协同机制的前提）
echo $A_STOCK_STATE_HOME        # 两端必须一致；未设置则都回退 ~/.hermes

# 3) 双跑审计（只读，不动状态）：确认无双写、state identity 一致
python scripts/dual_runtime_audit.py

# 4) 冒烟
python scripts/validate_cron_manifest.py cron/hermes-cron-manifest.json
python scripts/smoke_test.py
```

### 5.2 Hermes 端

**事实平面（含 research-dispatch）**：Hermes Gateway 以
`cron/hermes-cron-manifest.json` 为调度事实源，同步后新 job 自动生效；
Gateway cron 不健康时用既有 fallback：

```bash
python scripts/generate_system_crontab.py   # 输出系统 crontab 行，人工审阅后安装
```

**研究窗口（模型轮次）**：给 Hermes 加一条定时 agent 任务（如 20:30 交易
日），提示词模板：

```text
你是 a-stock-agent-system 的研究委员会执行者。
先读 skills/research-committee/SKILL.md。
循环执行 python scripts/expert_runner.py next --worker hermes：
拿到工单则按 instructions 完成推理并 submit；返回 idle 立即结束本轮。
除 expert_runner 的 submit/abstain/fail 外不得写任何文件。
```

### 5.3 OpenClaw 端

**事实平面（含 research-dispatch）**：命令型 cron 直跑 DAG（无模型冷启动），
用既有导出器全量对账注册（40 个 job 一次到位）：

```bash
export A_STOCK_STATE_HOME=<与 Hermes 相同的状态根>
python scripts/generate_openclaw_cron.py --reconcile          # 先审阅生成的命令
python scripts/generate_openclaw_cron.py --reconcile --apply  # 确认后应用
```

**研究窗口（模型轮次）**：加一条 agent 会话型 cron（与 Hermes 错峰，例如
21:00，两端同抢也安全——角色租约保证不双跑），消息体用 5.2 的模板并把
`--worker hermes` 换成 `--worker openclaw`；准确的 agent-payload 参数以本机
`openclaw cron create --help` 为准，命令型 job 的环境变量注入方式与导出器
生成的一致（`--command-env A_STOCK_STATE_HOME=...`）。

### 5.4 日常使用

```bash
# 人工发起研究（任一端执行效果相同）
python scripts/research_dispatch.py --kind user_request --code 600519 --reason "复核龙头地位"

# 看队列 / 单任务状态
python scripts/expert_runner.py status
python scripts/expert_runner.py status --task <task_id>

# 看产物
ls $A_STOCK_STATE_HOME/skills/research-committee/data/reports/
ls $A_STOCK_STATE_HOME/skills/research-committee/data/proposals/pending/
cat $A_STOCK_STATE_HOME/skills/research-committee/data/research_ledger.jsonl

# 兜底：黑板已集齐但合成未跑（如进程被杀）
python scripts/expert_runner.py synthesize
```

调参一律改 `config/research_committee.json`（top_k、亏损阈值、每日预算、
冷却天数、否决/冲突置信度、升级开关），无需改代码。

### 5.5 验证清单（部署机首日）

- [ ] `dual_runtime_audit.py` 无 concurrent_duplicate_runs，state_identity 一致
- [ ] 15:50 后 `cron/output/research-dispatch/` 出现 artifact
- [ ] 有触发时 `research_tasks.json` 出现 pending 任务；无触发时 dispatch
      输出 `has_signal: false`（静默）
- [ ] 研究窗口后任务进入终态，`reports/` 有当日报告
- [ ] `budget/{date}.json` 的 reserved 与 actuals 合理（首周用于校准预算）
- [ ] `proposals/pending/` 产物均带 `policy_gate_required: true`

### 5.6 回滚

研究平面是纯增量，回滚不影响事实平面：

1. manifest 中 `research-dispatch.enabled` 置 `false`（或 OpenClaw 端
   `openclaw cron disable "A-stock: research-dispatch"`）；
2. 停用两端研究窗口 agent 任务；
3. `skills/research-committee/data/` 保留为审计记录，不必删除。

---

## 6. 后续路线

- **M2**：报告投递接入 `delivery_policy` / novelty gate（当前报告只落盘）；
  触发器扩展（EOD 异动扫描 artifact、催化剂触发、performance-weekly 复盘）；
  proposal → strategy_registry 提案流的半自动衔接。
- **M3**：依据首周 budget actuals 校准预算与预估函数；评估开启有界升级；
  晚间批处理窗口（单 session 串烧多任务摊薄冷启动）；`runtime_affinity`
  标签按两端实测模型配额/时延调优；dual_runtime_audit 扩展覆盖研究任务。
