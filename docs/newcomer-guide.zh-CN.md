# A 股智能投研 Agent 系统：新人使用手册

适用版本：`main`（已包含 PAT P3–P6，核对日期 2026-08-10）

## 一句话理解

**把它当成“有证据、有风控、有复盘的投研工作台”，不要把它当成自动荐股或自动交易机器人。**

效果最佳的使用方式不是一次开启全部 62 个任务，而是按下面的顺序逐步使用：

```text
离线验证 → 单股分析 → 建立观察列表 → 录入真实持仓
→ 每日 DAG → 深度研究委员会 → 评测与复盘 → 稳定后再启用调度器
```

系统不会连接券商或下单。它会收集证据、生成候选和研究结论、执行确定性门禁、记录建议，
并用独立模拟账户观察结果。最终是否交易始终由用户决定。

## 1. 先认识三个平面

| 平面 | 做什么 | 新人应该如何使用 |
|---|---|---|
| 事实平面 | 拉取行情、公告、政策和资讯，生成带时间与来源的快照 | 先看数据是否新鲜、来源是否正常 |
| 研究平面 | Skill 和 Agent 解释有界证据，提出研究结论或分析计划 | 把结论当研究意见，不当事实 |
| 决策平面 | 执行可成交性、T+1、集中度、OOS 和风险门禁 | 重点阅读 `blocked/watch/avoid` 的原因 |

PAT P3–P6 增强了研究闭环，但没有扩大交易权限：

- P3 从失败和证据不足中提出评测候选，不能自动改代码或策略；
- P4 保存可追溯的研究数据和时点检索包，不能直接影响实时排名；
- P5 让 Agent 只能起草受限分析计划，确定性编译器负责检查和封装；
- P6 在隔离环境中执行两次并比较结果，验证通过仍只代表“研究计算可复现”。

## 2. 第一次使用：30 分钟安全上手

### 2.1 安装

要求 Python 3.10+，推荐使用独立虚拟环境：

```bash
git clone https://github.com/Eleven1111/a-stock-agent-system.git
cd a-stock-agent-system

python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[charts,fundamentals,research,dev]"
```

如果本机没有 `python3.12`，可替换为任意 Python 3.10+ 可执行文件。

### 2.2 建立独立状态目录

状态目录存放持仓、观察列表、快照、研究产物和 Ledger，不能放在 Git 仓库里：

```bash
export A_STOCK_STATE_HOME="$HOME/.a-stock-agent"
export A_STOCK_RUNTIME="local"
mkdir -p "$A_STOCK_STATE_HOME"
```

首次初始化时先不要设置 `A_STOCK_STATE_ID`：

```bash
unset A_STOCK_STATE_ID
python scripts/state_doctor.py --runtime local
```

命令会在状态目录生成唯一身份。需要接入 Hermes/OpenClaw 时，再把这个身份固定到环境：

```bash
export A_STOCK_STATE_ID="$(python -c 'import json,os; print(json.load(open(os.path.join(os.environ["A_STOCK_STATE_HOME"],"state_identity.json")))["state_id"])')"
```

不要复制其他机器的 `A_STOCK_STATE_ID`，也不要让两个不同目录使用同一个身份。

### 2.3 验证代码与配置

```bash
python scripts/config_doctor.py
python scripts/validate_cron_manifest.py cron/hermes-cron-manifest.json
```

预期结果：配置报告为 `status: ok`，Manifest 显示 `OK`。当前版本登记 62 个任务，47 个启用。

### 2.4 先跑离线示例

以下命令使用 fixture，不依赖实时行情，也不会修改真实持仓：

```bash
python skills/daban-stock-picker/scripts/daban_candidate_api.py --example --json
python skills/chanlun-backtest/scripts/research_gate.py --example --json
python scripts/evaluate_agent_harness.py --quiet
```

只有离线示例正常后，才进入联网分析。

### 2.5 检查数据源

```bash
python scripts/provider_doctor.py --json
```

重点看每个 dataset 的 `status` 和 `required`：

- required 数据源失败：相关分析应停止或返回 blocked；
- optional 数据源失败：相关维度会关闭或重新归一化；
- 空数据不是“没有风险”，也不能自动解释为中性。

## 3. 新人最推荐的三条使用路径

### 路径 A：研究一只股票

先做联网四维分析：

```bash
python skills/stock-triage/scripts/four_dim_scorer.py 600519 贵州茅台 --json
```

然后把股票加入观察列表：

```bash
python skills/stock-triage/scripts/monitor_manager.py \
  --add-stock 600519 贵州茅台
python skills/stock-triage/scripts/monitor_manager.py --list
```

如果不再关注，要显式取消：

```bash
python skills/stock-triage/scripts/monitor_manager.py \
  --cancel-stock 600519 --reason "用户不再关注"
```

手工取消会形成持久墓碑，自动发现任务不能偷偷把它重新加入。

适合提给 Hermes/OpenClaw 的问题模板：

> 先运行 `python scripts/agent_runtime_context.py`，再基于最新时点证据分析 600519。
> 请分开列出事实、推断、风险、未知项、入场条件、失效条件、持有周期和 T+1 约束；
> 不要把缺失数据解释为中性，不要从聊天记录猜测我的持仓。

### 路径 B：管理真实持仓，但不自动交易

先用已核验账户余额初始化现金。不要用估算值：

```bash
python skills/stock-triage/scripts/portfolio_manager.py \
  --reconcile-cash 100000 \
  --cash-source user_confirmed \
  --cash-asof 2026-08-10 \
  --json
```

录入已实际成交的持仓时，需要同时提供行业分类、来源和日期：

```bash
python skills/stock-triage/scripts/portfolio_manager.py \
  --add 600519 贵州茅台 1500.00 \
  --shares 100 \
  --sector 白酒 \
  --classification-source user_confirmed \
  --classification-asof 2026-08-10 \
  --json
```

这里的 `--add` 表示记录已经发生的交易，不是让系统下单。当天新增股份会被 T+1 锁定，
系统不会建议当天卖出。

查看账户与风控：

```bash
python skills/stock-triage/scripts/portfolio_manager.py --balance --json
python skills/stock-triage/scripts/portfolio_manager.py --check --json
python scripts/agent_runtime_context.py --full
```

实际清仓后再登记：

```bash
python skills/stock-triage/scripts/portfolio_manager.py \
  --close 600519 1580.00 --json
```

不要直接手改 `portfolio.json`、`signal_ledger.jsonl` 或 `monitor_registry.json`。这些文件有
事件投影、T+1 lot、审计 lineage 和墓碑语义，绕过 CLI 容易制造不一致。

### 路径 C：运行每日研究链路

手工运行时仍应从统一 DAG 入口进入，不要直接拼接一串业务脚本：

```bash
# 盘前情报
python scripts/run_agent_dag.py preopen-intelligence-brief \
  --runtime local --emit-target

# 09:35 后的开盘确认
python scripts/run_agent_dag.py open-intelligence-brief \
  --runtime local --emit-target

# 收盘筛选与组合检查
python scripts/run_agent_dag.py closing-triage \
  --runtime local --emit-target

# 每日诊断
python scripts/run_agent_dag.py daily-diagnostics \
  --runtime local --emit-target
```

DAG 会自动解析必需依赖、交易日、batch、租约、快照和质量门禁。某个上游返回 blocked 时，
下游不会假装成功。

建议新人先手工运行 3–5 个交易日，确认状态目录、数据源和输出符合预期，再考虑安装调度器。

## 4. 如何使用研究委员会

研究委员会适合“证据复杂、观点冲突、需要红队”的问题，不适合替代每次简单查询。

```bash
python scripts/research_dispatch.py \
  --kind deep_debate \
  --code 600519 \
  --reason "复核基本面证据链、反方风险与失效条件"

python scripts/expert_runner.py status
```

Hermes/OpenClaw 可以通过同一个 research bus 认领不同角色：

```bash
python scripts/expert_runner.py next --worker hermes
python scripts/expert_runner.py next --worker openclaw
```

模型 Runtime 必须真正完成对应 turn 并写回合规 finding；`next` 本身不会凭空生成研究内容。
任务达到 ready 后再做确定性合成：

```bash
python scripts/expert_runner.py synthesize
```

最值得看的是：

- 引用的 evidence ref 是否真实存在且时点正确；
- `risk_redteam` 是否提出足以否决结论的证据；
- 正反证据是否被同时保留；
- 最终是 finding、abstain 还是 blocked；
- 是否仍标记 `research_only`、`execution_eligible=false`。

## 5. P3–P6 什么时候使用

### P3：把失败变成离线评测案例

```bash
python scripts/learning_eval_factory.py scan
python scripts/learning_eval_factory.py status
```

扫描只会提出候选。必须由人审核并补齐冻结 benchmark 后，才能导出评测集。即使评测通过，
也不代表策略具有投资收益或可以自动上线。

### P4：写入研究资料并做时点检索

```bash
python scripts/research_data_plane.py ingest-document \
  --document /path/to/document.json

python scripts/research_data_plane.py query \
  --query "股份回购" \
  --asof "2026-08-10T15:00:00+08:00" \
  --allowed-scope public
```

检索包会保留来源、可用时点、访问范围和冲突证据。查询没有结果只表示“未检索到证据”。

### P5/P6：受限计划编译与确定性执行

P5/P6 主要面向研究 Runtime 集成者，不是新人日常选股命令。正确顺序是：

```text
现有研究 proposal
→ Agent 只起草 analysis plan
→ 编译器检查 schema、DAG、类型和算子白名单
→ P6 重验全部 hash
→ 两个隔离工作区重复执行
→ 结果一致才生成 validation evidence
```

P6 的 CLI 需要已经编译的 handoff、输入和 dataset catalog：

```bash
python scripts/execute_compiled_analysis.py \
  --compilation /path/to/dual-agent-compilation.json \
  --inputs /path/to/execution-inputs.json \
  --catalog config/dataset_catalog.json \
  --validated-at "2026-08-10T10:00:00+08:00" \
  --output /path/to/execution-result.json
```

`status=validated` 只证明这次研究计算在受限条件下可复现，不证明输入事实绝对正确、预测会
实现或策略已获准影响实时决策。

## 6. 怎样读系统输出

每次先看状态，再看分数：

| 输出 | 含义 | 应对 |
|---|---|---|
| `ok` / `validated` | 本次工程契约通过 | 继续检查证据、时点和风险，不等于可以买 |
| `watch` / `conditional` | 条件未完全满足 | 写清需要等待什么，不提前行动 |
| `avoid` | 触发明确风险或不可成交门禁 | 不要用模型“说服”系统绕过 |
| `blocked` | 缺少必需证据、权限、依赖或验证 | 修复原因后重跑，不把它改写为中性 |
| `insufficient_data` | 数据不足以支持结论 | 补数据或保持未知 |
| `abstain` | Agent 主动不下结论 | 这是合格结果，不是失败 |
| `research_only=true` | 仅供研究 | 不得进入实盘排序或执行 |
| `trading_action=none` | 没有交易动作权限 | 不应被外部调用者解释为订单 |

一个方向性建议至少应回答：

1. 证据截至什么时间，来自哪里？
2. 哪些是事实，哪些是模型推断？
3. 入场条件和最高追价是什么？
4. 什么条件使结论失效？
5. 仓位上限和组合集中度如何？
6. 最短持有周期、T+1 最早卖出日是什么？
7. 哪些关键数据未知、过期或不可用？

缺少其中任一关键项时，优先要求补齐，而不是只看总分或 confidence。

## 7. 每日最佳实践

### 盘前

1. 运行 `provider_doctor.py --json` 检查必需数据源；
2. 运行盘前 DAG，查看全球市场、公告和候选依赖是否完整；
3. 只把 `conditional_buy` 当成待确认条件，不提前解释为买入。

### 开盘后

1. 等待 09:35 开盘确认重新检查价格、可成交性和公告；
2. 对真实持仓先运行 `agent_runtime_context.py`；
3. 任何新增持仓都要遵守 T+1，不计划当日卖出。

### 盘中

1. 只监控持仓和显式订阅目标；
2. 高频任务无触发时保持静默是正常行为；
3. 数据源失败、空返回和无风险不是同一件事。

### 收盘后

1. 运行收盘筛选、组合检查和执行纪律复盘；
2. 把实际成交登记到组合状态，不从聊天推断；
3. 查看 T+1 provisional 与 T+3 final 结算，不用单日涨跌评价策略；
4. 周期性检查策略注册表、OOS 和 shadow 结果。

## 8. 什么时候再启用自动调度

满足以下条件后再阅读 [`AUTOPILOT.md`](../AUTOPILOT.md) 安装调度器：

- 手工 DAG 已连续运行至少 3–5 个交易日；
- `A_STOCK_STATE_HOME` 已固定并完成备份；
- Provider doctor 的必需数据源稳定；
- 已理解哪些任务是 research-only、哪些任务会写状态；
- 同一台机器只选择一个正式调度入口；
- Hermes 与 OpenClaw 使用同一状态根和状态身份；
- 能独立完成停止、日志检查、任务对账和回滚。

不要直接复制 `AUTOPILOT.md` 中某台已部署机器的绝对路径。跨机器共享本地文件目录也不能
提供分布式排他；当前受支持的稳妥拓扑是同一台主机、同一状态根、统一调度入口。

## 9. 常见问题

### 为什么分数不错却没有买入建议？

总分只是研究输入。公告、数据质量、可成交性、价格计划、组合风险、策略注册或 OOS 任一
门禁不通过，都应阻止正向建议。

### 为什么返回 blocked，而不是给一个模糊答案？

这是系统的核心安全设计。blocked 把缺失条件显式暴露出来，避免模型用语言流畅度掩盖
证据缺口。

### 为什么 `state_doctor` 返回 degraded？

先看 `identity.status`。如果它是 `ok`，但 `split_brain.detected=true`，说明机器上发现了多个
不同的历史状态身份。不要直接删除或合并目录；先核对每个 `identities[].root`，确定哪一个
才是当前正式状态根。统一 Hermes/OpenClaw 的环境配置后再重跑。`degraded` 是需要处理的
运维提醒，不应被静默忽略。

### 可以同时让 Hermes 和 OpenClaw 跑吗？

同机可以，但必须共享 `A_STOCK_STATE_HOME` 和 `A_STOCK_STATE_ID`，并依赖同一套租约和
Ledger。多机并发不是当前本地文件方案的安全拓扑。

### 可以让系统根据聊天记录自动维护持仓吗？

不可以。持仓、监控和取消项必须来自明确 CLI 操作或规范运行时文件，不能从对话猜测。

### 为什么测试通过仍不能说策略有效？

测试证明代码和契约按预期工作；它不证明线上已采用、不证明真实数据永远正确，也不证明
未来收益。策略仍需 OOS、成本、对照组、shadow、对账和人工审批。

## 10. 新人检查清单

首次使用：

- [ ] 使用 Python 3.10+ 独立虚拟环境
- [ ] 设置仓库外的 `A_STOCK_STATE_HOME`
- [ ] 完成 `state_doctor`、`config_doctor` 和 Manifest 验证
- [ ] 离线示例通过
- [ ] Provider doctor 已运行并理解 required/optional

每次研究：

- [ ] 证据时间和来源可见
- [ ] 事实、推断、风险和未知项分开
- [ ] 没有把 blocked/空数据解释为中性
- [ ] 方向性建议包含价格、失效、仓位、周期和 T+1
- [ ] `research_only` 产物没有被误当成实盘信号

启用自动化前：

- [ ] 手工 DAG 连续稳定运行
- [ ] 状态根、身份、备份和隐私边界明确
- [ ] 只保留一个正式调度入口
- [ ] 会检查实际安装状态，而不是只看 Git 中的 Manifest
- [ ] 会停止、对账和回滚

## 继续阅读

- [交易与监控生命周期](trading-lifecycle.md)
- [研究委员会使用指南](research-committee-guide.md)
- [运行时架构与加固](architecture-hardening.md)
- [模拟交易协议](paper-trading-protocol.md)
- [Learning Ledger 与 Eval Factory](learning-eval-factory-v1.md)
- [P4 研究数据与 RAG](pat-p4-research-data-rag.md)
- [P5 双 Agent 编译链](pat-p5-dual-agent-compiler.md)
- [P6 确定性执行与验证](pat-p6-execution-validation.md)
