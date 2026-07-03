# 多专家研究模式使用指南（新手向）

面向对象：不熟悉这套系统内部机制、但想搞懂"多专家到底怎么跑的"、以及"我想让
某个专家变聪明该改哪里"的人。读完本文你应该能：看懂一次研究任务从触发到出
报告的全过程；说清楚三个专家各自在想什么；自己动手改一个专家的能力，并知道
怎么验证改得对不对。

不涉及代码原理，只讲"怎么用、怎么改"。

---

## 0. 三句话先建立直觉

1. **专家不是随时待命的**：没有触发条件，系统什么都不做，也不花一分钱 token。
2. **专家互相看不见对方的发言**：三个专家各自写自己的结论，写完之后由一段
   固定的代码逻辑（不是模型）来裁决最终结论，不存在"专家们开会讨论"这种事。
3. **专家永远不能自己下结论去买卖**：他们的结论最多变成一份"提案"，还要再过
   系统里原有的风控关卡才可能生效。这份指南里改的所有东西都不会绕开风控。

---

## 1. 一次任务是怎么跑完的（全流程）

用一个具体例子走一遍：假设候选池里出现了一只新股票，系统觉得值得深入研究。

```text
① 触发
   系统每天收盘后自动扫一遍"有没有值得研究的东西"
   （候选池里排名靠前的股票 / 行为异常 / 亏损复盘 / 你手动发起的请求）
   → 有 → 生成一个"研究任务"，放进任务队列
   → 没有 → 什么都不发生，安静收工

② 领工单（专家出场）
   Hermes 或 OpenClaw 在自己的"研究时间窗口"里，会执行一条命令去问：
   "队列里有没有我能做的活？"
   → 有 → 领到一个工单：具体是哪个任务、扮演哪个角色、
          还附带一份"证据材料包"（这只股票相关的所有已知事实，打包好、有字数上限）
   → 没有 → 回答"idle"（闲着），本轮结束

③ 专家干活
   模型读工单里的"角色说明书"（下面会全文给出）+ 证据材料包，
   给出一个结论：支持 / 反对 / 中立 / 弃权，并写清楚理由

④ 交卷
   模型把结论按固定格式提交。系统会先检查格式对不对（比如"说支持就必须
   给反方证据"），格式不对会被打回重写。

⑤ 集齐才裁决
   一个任务通常要 2-3 个专家都交卷，系统才会自动裁决。
   裁决规则是写死的代码逻辑，不是让另一个模型来"总结一下"：
   - 风险专家强烈反对 → 直接否决，任务结束
   - 大家意见吵得很凶（都很确信，但方向相反）→ 如实标记"有分歧"，不装作有共识
   - 有专家看好且没人强烈反对 → 生成一份"提案"
   - 都不看好也不反对 → 只留观察记录，不生成提案
   - 大家都说"证据不够无法判断" → 任务标记"弃权"结束

⑥ 出成果
   一份 Markdown 研究报告（谁说了什么、结论是什么、反方证据是什么）
   如果结论是"值得推进" → 额外生成一份提案文件，
   但这份提案上明确写着"必须再过风控关卡才能生效"——多专家系统到这里就结束了。
```

**关键认知**：③ 是唯一花 AI token 的环节，其余全是确定性代码在跑。这就是为什么
系统"安静"的时候是真的零成本，不会因为挂着就一直烧钱。

---

## 2. 认识三个专家

现在系统里有三个专家，每个专家的"人设"就是一个纯文本文件（角色 profile），
存放在 `skills/research-committee/experts/` 目录下。**模型看到的就是这个文件的
原文**，没有隐藏的额外指令。

### 2.1 证据审计（evidence_auditor）

文件：[`skills/research-committee/experts/evidence_auditor.md`](../skills/research-committee/experts/evidence_auditor.md)

一句话人设：**挑证据材料本身的毛病**，不判断股票好不好，只判断"我们掌握的
信息靠不靠谱"。

> 职责：
> 1. 审计证据链本身：数据是否齐全、是否过期、生成时间与任务日期是否对得上。
> 2. 交叉核对：候选池、评分摘要、资金面摘要之间是否互相矛盾。
> 3. 解读市场状态证据的强度：情绪/资金结论有没有数据支撑。
>
> 硬规则：只能用工单给的材料，不能凭记忆或上网查；发现过期/缺失的关键证据，
> 最高只能给"中立"评价，并把这个问题记录下来；不做买卖建议。

**什么时候会弃权**：材料残缺到没法判断证据链是否可信时。

### 2.2 论点构建（thesis_builder）

文件：[`skills/research-committee/experts/thesis_builder.md`](../skills/research-committee/experts/thesis_builder.md)

一句话人设：**正方**。负责把"这只股票为什么值得研究"的逻辑讲清楚，但讲的
同时必须自己先挑自己的刺。

> 职责：
> 1. 评估题材强度与轮动位置。
> 2. 梳理传导链：政策/新闻 → 产业链 → 该标的的受益逻辑是否成立。
> 3. 评估龙头地位与预期差。
>
> 硬规则：只能用工单给的材料；**如果结论是"支持"，必须同时给出反证和
> 失效条件——没有反证就不能算结论**；不能因为"题材看起来热"就无视材料里的
> 负面信息；不做买卖建议。

**这条规则很重要**：thesis_builder 表面上是"看多的人"，但系统逼着他必须先
自己反驳自己一次，不允许只唱赞歌。

### 2.3 风险红队（risk_redteam）

文件：[`skills/research-committee/experts/risk_redteam.md`](../skills/research-committee/experts/risk_redteam.md)

一句话人设：**唯一有一票否决权的人**。任务是攻击论点，不是完善论点。

> 职责：
> 1. 主动搜索反证：资金流出、情绪退潮、评分与叙事背离、行为风险信号。
> 2. 攻击可交易性与结构风险：涨跌停约束、流动性、拥挤度、公告风险。
> 3. 检查失效条件是否可核验：说得含糊的失效条件（"走弱就撤"）视为无效。
> 4. 如果是"升级轮"（专家们吵起来了，系统给了第二次机会），必须正面回应
>    对方最强的论据，不能重复上一轮的话。
>
> 否决权：**只要他给出"反对"且信心 ≥ 0.7，任务直接被否决**，这是全委员会
> 唯一的一票否决。但硬规则要求他必须有具体证据支撑才能否决，不能凭直觉；
> 遇到真风险也不允许为了"和气"而调低信心蒙混过关。

**这是整套系统里风控意识最重的角色**——如果你只想升级一个专家、又不确定改
哪个，通常应该先看这个。

---

## 3. 专家怎么"说话"：输出格式

三个专家不管扮演什么角色，交卷时用的是同一套格式（这样系统才能自动裁决）。
用大白话讲，专家必须回答清楚这几件事：

| 字段 | 大白话 | 举例 |
|---|---|---|
| `stance` | 我的立场 | 支持 / 反对 / 中立 / 弃权 |
| `confidence` | 我有多确信（0~1） | 0.8 |
| `summary` | 一句话结论 | "资金面证据与候选逻辑冲突，行为风险偏高" |
| `evidence_refs` | 我的结论基于材料包里的哪几条 | "证据包里的资金流摘要" |
| `counterevidence` | （立场=支持时必填）我自己找的反方证据 | "资金流入集中在单日，持续性未证实" |
| `invalidation_conditions` | （立场=支持时必填）什么情况出现就说明我错了 | "跌破 20 日线" |
| `risk_flags` | 风险标记（谁都可以写） | "资金持续净流出" |
| `abstain_reason` | （立场=弃权时必填）为什么判断不了 | "证据包缺少关键成交数据" |

**如果专家瞎编、漏填必填项，系统会直接拒收，不会进入下一步。** 这不是"建议",
是硬校验，格式不对交不了卷。

---

## 4. 现在有几种任务会触发专家出动

写在 `config/research_committee.json` 里的 `triggers` 部分：

| 任务类型 | 触发条件 | 出动的专家 |
|---|---|---|
| `candidate_deep_dive`（候选深研） | 每天候选池排名前 2 的股票 | 证据审计 + 论点构建 + 风险红队 |
| `anomaly_review`（异常复核） | 系统检测到"行为风险"等级为高/严重 | 风险红队 |
| `postmortem`（复盘） | 某只票最终结算亏损超过 5%（每天最多查 1 只） | 证据审计 + 风险红队 |
| `user_request`（人工请求） | 你手动发起 | 证据审计 + 论点构建 + 风险红队 |

你随时可以自己手动发起一次研究，不用等触发条件：

```bash
python scripts/research_dispatch.py --kind user_request --code 600519 --reason "复核龙头地位"
```

查看队列里现在有哪些任务：

```bash
python scripts/expert_runner.py status
```

---

## 5. 怎么升级一个（或几个）专家的能力

按改动的"侵入程度"从小到大排列。**新手建议只做到第 2 层**，第 3、4 层需要
稍微理解一下配置文件结构，但依然不用碰任何代码。

### 第 1 层：改"人设说明书"（最安全，最推荐）

这是最直接的升级方式——**专家的"能力"本质上就是那份 profile 文件里写的话**。
想让某个专家更严格、更懂某个细分领域、检查更细致的点，直接编辑对应文件：

```text
skills/research-committee/experts/evidence_auditor.md
skills/research-committee/experts/thesis_builder.md
skills/research-committee/experts/risk_redteam.md
```

**例子：想让风险红队专门多看一眼"游资打板"相关的结构风险**

打开 `risk_redteam.md`，在"职责"部分加一条：

```markdown
2. 攻击可交易性与结构风险：涨跌停约束、流动性、拥挤度、公告风险的任何迹象。
   如果证据包里出现"连续涨停"或"龙虎榜"相关信息，必须额外评估断板风险和
   高位接盘风险，并在 risk_flags 里单独标出"高位接力风险"。
```

改完不需要重启任何服务，也不需要跑测试——这只是一段文本，下次有专家领到
这个角色的工单时，读到的就是新版本。

**怎么验证改得有没有效**：手动发起一次研究任务，然后自己扮演一次这个专家看
看工单长什么样（下面第 6 节有具体步骤）。

### 第 2 层：改"能看到多少材料"和"判断标准松紧"

打开 `config/research_committee.json`，这是所有可调参数的唯一入口。

**给某类任务的专家看更多/更少材料**（改 `pack_jobs`）：

```json
"candidate_deep_dive": {
  "experts": ["evidence_auditor", "thesis_builder", "risk_redteam"],
  "pack_jobs": [
    "closing-triage",
    "four-dim-scorer",
    "candidate-discovery",
    "hot-money-context",
    "capital-flow"
  ]
}
```

`pack_jobs` 里每一项是系统里某个定时任务的产出摘要，会打包进证据材料。想让
专家看到"机构调研"相关信息，就把 `institution-weekly` 加进这个列表；想让
证据包更精简（省 token），就删掉不重要的项。

**加大或收紧材料预算**（改 `pack_budget_chars`）：

```json
"pack_budget_chars": 24000
```

这是这类任务的证据材料"最大字数"，改大了专家能看到更完整的信息，但每次
花的 token 也更多；改小了会触发系统自动做"材料精简"（丢弃不重要的部分）。

**调整裁决松紧**（改 `synthesis` 部分）：

```json
"synthesis": {
  "veto_confidence": 0.7,
  "conflict_confidence": 0.6,
  "advance_min_support_confidence": 0.6
}
```

- `veto_confidence`：风险红队要多确信才能一票否决。调低 → 更容易被否决（更保守）；
  调高 → 否决更难触发（更激进）。
- `conflict_confidence`：正反双方要多确信才会被判定为"分歧"而不是随便一方胜出。
- `advance_min_support_confidence`：支持方至少要多确信，任务才会被判定为
  "值得推进生成提案"。

改完这个文件**不需要重启任何东西**，下一次任务读的就是新配置。

### 第 3 层：给专家一次"复议机会"（有界升级）

系统内置了一个功能：如果专家们意见分歧很大，可以让冲突的两方"重新看一遍
对方最强的论据，再给一次结论"，而不是直接不了了之。这个功能默认是关的。

打开它：

```json
"synthesis": {
  "escalation": {
    "enabled": true,
    "max_rounds": 1
  }
}
```

`max_rounds` 是最多复议几轮（防止两个专家没完没了地吵，浪费 token）。建议
先设成 1，观察一段时间效果稳定了再考虑要不要加。

### 第 4 层：新增一个专家角色

如果三个专家角色不够用，比如想加一个专门看"政策解读"的专家，步骤是：

1. **写角色说明书**：在 `skills/research-committee/experts/` 下新建一个文件，
   比如 `policy_reader.md`，格式照抄现有三个文件的结构（职责 + stance 语义
   + 硬规则）即可。

2. **在配置里登记这个专家**（`config/research_committee.json` 的 `experts` 部分）：

   ```json
   "experts": {
     "policy_reader": {
       "profile": "skills/research-committee/experts/policy_reader.md",
       "max_output_chars": 4000
     }
   }
   ```

3. **把它加进某个任务类型的专家计划里**（比如加进 `candidate_deep_dive`）：

   ```json
   "candidate_deep_dive": {
     "experts": ["evidence_auditor", "thesis_builder", "risk_redteam", "policy_reader"]
   }
   ```

**不需要改任何 Python 代码**——系统的专家执行逻辑（`scripts/expert_runner.py`）
是"通用的"，它不知道具体角色叫什么名字，只是照着配置文件读取对应的说明书，
所以加角色纯粹是配置文件层面的事。

> 注意：如果新专家也应该拥有"一票否决权"，目前只有 `risk_redteam` 这个
> 角色名硬编码了否决逻辑（在 `research_synthesis.py` 里）。这属于代码改动，
> 不在本指南范围内，需要找开发者改。单纯"新增一个只发表意见、不否决"的
> 专家，完全不需要碰代码。

### 第 5 层：新增一种任务类型 / 触发条件

如果你想让系统在新的情况下自动发起研究（比如"北向资金单日净流出超过 50 亿"
时自动分析），需要：

1. 在 `task_kinds` 里新增一个任务类型定义（参考现有四个的写法）。
2. 在 `triggers` 里加一条触发规则。

但触发规则的"判断逻辑"（比如怎么算"北向资金净流出超过 50 亿"）目前是写在
`scripts/research_dispatch.py` 代码里的，新增一种全新的判断逻辑需要改代码。
如果只是想复用已有的判断方式（候选池排名 / 行为风险等级 / 结算亏损）去配置
新任务类型，改配置文件即可。

---

## 6. 改完之后怎么验证（不用连 Hermes/OpenClaw 也能测）

### 6.1 最简单：自己走一遍工单流程

```bash
# 1. 手动发起一个研究任务
python scripts/research_dispatch.py --kind user_request --code 600519 --name 测试股票 --reason "验证专家改动"

# 2. 领一个工单，看看专家会读到什么内容
python scripts/expert_runner.py next --worker manual-test
```

第二条命令会打印出完整的"工单"，包括：
- `instructions`：专家会读到的完整说明书原文（改了 profile 文件的话，这里
  能立刻看到新内容）
- `evidence_pack`：这次专家能看到的所有证据材料
- `output_contract`：专家必须遵守的输出格式

**这一步不消耗任何 AI token**，纯粹是让你确认"改动生效了、材料对不对"。

### 6.2 手动模拟一次专家交卷

把工单里的角色和任务 ID 记下来，自己写一份 JSON 模拟专家的结论：

```bash
cat > /tmp/finding.json <<'EOF'
{
  "schema": "research_finding_v1",
  "task_id": "<从上一步的工单里复制 task_id>",
  "role": "<从上一步的工单里复制 role>",
  "stance": "support",
  "confidence": 0.8,
  "summary": "测试用结论",
  "evidence_refs": ["fact_artifacts.closing-triage"],
  "counterevidence": ["测试反证"],
  "invalidation_conditions": ["测试失效条件"]
}
EOF

python scripts/expert_runner.py submit --task <task_id> --role <role> --file /tmp/finding.json
```

如果格式有问题（比如你改的规则要求某个字段必填，但示例里没给），这一步会
直接报错并告诉你缺什么——用这个办法可以验证"新增的硬规则是否真的会被
系统强制执行"。

### 6.3 跑一次自动化测试（进阶，确保没弄坏整个系统）

如果你的改动**只是文本类改动**（profile 文件、config 里的数字），一般不需要
跑测试。如果你连着改了代码（比如第 4/5 层涉及代码的部分），跑一下：

```bash
pytest tests/test_research_bus.py tests/test_evidence_pack.py tests/test_research_synthesis.py tests/test_expert_runner.py tests/test_research_dispatch.py -q
```

全部通过说明没有破坏现有机制。

---

## 7. 几个容易踩的坑

1. **改 profile 文件后忘了改字数限制**：如果你让专家说明书变得很长，或者
   要求专家输出更详细的内容，记得同步调大 `config/research_committee.json`
   里对应专家的 `max_output_chars`，否则专家的回答可能会被截断。

2. **不要在 profile 里让专家"上网查资料"或"回忆之前的对话"**：三个现有
   专家的硬规则里都明确写了"只能用工单里的材料"，这是为了保证同一份材料、
   不同专家给出的结论可以互相比较。如果放开这条限制，裁决逻辑会失去意义。

3. **不要让专家自己决定要不要写入系统状态**：所有专家的硬规则最后一条都是
   "除交卷命令外，禁止写任何文件或状态"。这条不要删——多专家系统的产出
   永远只能是"建议"，不能是"事实"。

4. **调低否决阈值前想清楚**：`veto_confidence` 调得越低，风险红队越容易一票
   否决任务。这个参数本质上是"系统有多怕风险"的旋钮，不建议在没有观察过
   一段时间实际运行效果之前随意调整。

5. **加新专家不会自动获得否决权**：只有 `risk_redteam` 硬编码了否决逻辑，
   新增的专家默认只是"发表意见"，这是有意设计——否决权不应该随便扩散。

---

## 8. 速查表

| 我想做的事 | 改哪个文件 | 要不要碰代码 |
|---|---|---|
| 让某个专家关注更细的点 / 更严格 | `skills/research-committee/experts/*.md` | 否 |
| 让专家看到更多/更少证据材料 | `config/research_committee.json` 的 `pack_jobs` | 否 |
| 调整判断松紧（否决/分歧/推进阈值） | `config/research_committee.json` 的 `synthesis` | 否 |
| 打开"分歧复议"功能 | `config/research_committee.json` 的 `escalation` | 否 |
| 新增一个只发表意见的专家 | 新建 profile 文件 + 登记进 `config/research_committee.json` | 否 |
| 新增一个有一票否决权的专家 | 上面步骤 + `skills/common/research_synthesis.py` | 是 |
| 新增全新的触发判断逻辑 | `scripts/research_dispatch.py` | 是 |
| 调整触发条件的数值（如亏损阈值、top_k） | `config/research_committee.json` 的 `triggers` | 否 |

---

## 9. 更深入的资料

- 完整架构设计与实现细节：[research-plane-worklog-2026-07-03.md](research-plane-worklog-2026-07-03.md)
- 运行时操作手册（给 Hermes/OpenClaw 模型自己看的）：
  [skills/research-committee/SKILL.md](../skills/research-committee/SKILL.md)
- 三个专家的完整原文：`skills/research-committee/experts/`
- 所有可调参数：`config/research_committee.json`
