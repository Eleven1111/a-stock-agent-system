# PAT 借鉴 P0–P2 执行计划

状态：执行中  
基线：`main@dbda8b181100823710a501bab3002c60018df357`  
范围：研究基础设施与研究评估；不改变实时排名，不生成交易动作。

## 目标

借鉴 Bridgewater PAT 的类型化研究计划、确定性执行、数据语义契约和可验证反馈，优先补齐本仓库已经暴露的运行闭环。所有新增能力默认 `research_only`，并沿用现有 research bus、evidence pack、market snapshot 和 strategy registry。

## 交付顺序

### PR1 — P0 时间确定性

- 为官方政策监控注入统一 `checked_at`。
- freshness、seen-state 和 novelty gate 使用同一时钟。
- 固定测试时钟并覆盖第 44、45、46 天及未来日期。
- 不改变生产默认行为；未注入时仍使用北京时间。

验收：目标测试和完整测试通过，重复运行不受机器当前日期影响。

### PR2 — P0 研究消费者闭环

- 增加单次、受限的 shadow research consumer，不增加常驻守护进程。
- 使用 research bus 的 claim/lease/fencing 语义领取任务。
- 通过现有 runtime adapter 调用外部提供的研究 turn。
- 明确提交 `submitted`、`abstained`、`retryable_error` 或 `blocked`，不得把模型输出写入事实层。
- 输出 run artifact 和队列年龄/结果指标。

验收：覆盖 `enqueued -> claimed -> submitted/abstained`、租约冲突、重复执行、异常重试；未配置真实 runtime 时失败关闭。

### PR3 — P1 数据语义契约

- 增加 `dataset_contract_v1` 与 `dataset_catalog_v1`。
- 契约包含提供方、字段语义、单位、频率、时点约束、覆盖率、来源等级、谱系和已知限制。
- 增加严格验证器与版本/hash 绑定。
- 首批登记当前方向评估所需的市场快照字段，不扩张新的数据源。

验收：未知字段、单位冲突、缺少时点信息、重复数据集 ID 均失败关闭；合法目录可稳定计算内容哈希。

### PR4 — P2 受限分析计划

- 增加 `analysis_plan_v1`，只允许白名单确定性算子。
- 首个垂直切片为“策略方向与候选漏斗诊断”。
- 复用跨截面方向、发现召回与现有快照能力。
- 节点输入绑定数据集契约版本/hash，输出包含验证结果和执行谱系。
- 禁止任意 Python、任意模块导入和实时排名写入。

验收：覆盖合法 DAG、未知算子、循环依赖、输入 schema 不匹配、数据集 hash 不匹配和确定性重放。

## 共同门禁

- 先写失败测试，再写最小实现。
- 每个 PR 单独运行目标测试、Ruff、manifest 校验和 `git diff --check`。
- 大范围变更完成后运行完整 pytest；Python 3.10 兼容性按仓库 interpreter matrix 复核。
- 每个 PR 至少执行一个非 happy-path 对抗性探测。
- PR 未合并前不把后续能力描述为生产已采用。

## 停止条件

- 任何实现试图直接产生交易动作或绕过 strategy registry，立即停止。
- 真实 runtime 不可用时保留 shadow/abstain，不伪造研究结果。
- 数据契约无法证明时点与单位时，分析节点必须 blocked。
- P2 不引入新的 Agent 编排依赖；现有状态机不足的证据出现后再单独评估。
