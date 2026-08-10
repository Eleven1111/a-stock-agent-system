# PAT P4：研究数据写回与检索底座

状态：已合并 `main`（PR #202）
范围：仅研究平面；不改变实时排名、策略准入或交易执行。

## 为什么这样借鉴

PAT 的 compounding 与 RAG 值得借鉴，但本仓库不能把“模型产物被保存”直接等同于
“可供决策使用的数据”。P4 因此把写回和检索拆成两条严格契约：

1. 派生数据只有在确定性验证通过、验证结果与 records 哈希绑定、输入谱系完整时才能
   写入内容寻址存储。
2. 研究文档只有在来源、许可、可用时点和访问范围完整时才能入库；查询时再次按
   `asof` 和调用者 scope 失败关闭。

两类产物都固定为 `research_only=true`、`trading_action=none`。它们不能自动进入实时
排序，派生数据还必须保持 `pending_catalog_review`，经目录审核后才可能成为后续分析
计划的输入。

## 数据写回契约

`skills/common/derived_research_store.py` 写入 `derived_dataset_v1`：

- `records_hash` 绑定实际 records；
- `validation.records_hash` 必须与之相同；
- `validation.artifact_sha256` 必须匹配验证描述本身；
- `plan_hash`、`catalog_hash`、输入契约 hash、快照 ref 和算子版本构成 lineage；
- `point_in_time_cutoff` 与 `available_at` 必须带时区，且后者不能早于前者；
- 最终 artifact 以规范 JSON 的 SHA-256 命名，重复写入幂等，读取时重新验 hash。

这使写回成为“可复现研究资产”，而不是覆盖式缓存。内容相同但验证、谱系或可用时点
不同，都会生成不同 ref。

## 检索契约

`skills/common/research_retrieval.py` 提供：

- 内容寻址的 `research_document_v1`；
- `published_at <= asof` 且 `available_at <= asof` 的 point-in-time 过滤；
- 文档 access scopes 必须是调用者 scopes 的子集；
- 词法相关性、可选语义分数和来源权威度的确定性混合排序；
- 同一 claim 的支持/反对证据并存，不做“多数票消冲突”；
- 摘要使用 `untrusted_external_data` 结构包裹，不能被当成 Agent 指令；
- 查询结果固化为可验 hash 的 `retrieval_bundle_v1`，只暴露有界摘要与引用，不复制
  全文到下游上下文。

当无结果时，`absence_means_no_evidence=false` 明确表示“没有检索到证据”不能推导为
“没有风险”或“事实不存在”。外部语义检索器只能提交 `[0, 1]` 的文档 ID 分数表，不能
改写来源、时点或访问控制。

## 操作入口

统一入口为 `scripts/research_data_plane.py`：

```bash
python scripts/research_data_plane.py ingest-document \
  --document /path/to/document.json

python scripts/research_data_plane.py query \
  --query '股份回购' \
  --asof '2026-08-10T15:00:00+08:00' \
  --allowed-scope public

python scripts/research_data_plane.py write-derived \
  --dataset-id direction_summary_v1 \
  --records /path/to/records.json \
  --lineage /path/to/lineage.json \
  --validation /path/to/validation.json \
  --point-in-time-cutoff '2026-08-08T15:00:00+08:00' \
  --available-at '2026-08-10T09:30:00+08:00'
```

默认存储仍由 `A_STOCK_STATE_HOME` 和 `skills/common/paths.py` 决定。CLI 可用显式
`--store-dir` 做隔离测试，但生产运行不应创建第二套状态根。

## P5/P6 接口边界

- P5 的交互 Agent 只能选择/引用 retrieval bundle；代码执行 Agent 只接受类型化计划和
  bundle ref，不接受自由文本直接执行。
- P6 的执行器必须在消费前重新验证 bundle、dataset、plan 和 validation hash，并将
  实际输入 ref 写入执行证据。
- 本阶段没有引入向量数据库或新的 Agent。语义分数是受约束的可选输入；没有可信
  embedding 服务时，词法 + 来源等级路径仍可确定性运行。

## 已知限制

- 当前检索是单机文件存储，不提供跨机器事务与分布式租约。
- 当前中文分词采用标准库字符/双字粒度，适合作为确定性地板，不等同于领域向量召回。
- 来源等级是排序先验，不替代 citation correctness 或事实核验。
- “代码存在且测试通过”只证明工程能力，不证明已接入全部生产研究流量或产生投资收益。
