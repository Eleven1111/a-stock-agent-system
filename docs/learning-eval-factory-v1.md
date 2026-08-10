# Learning Ledger 与 Eval Factory v1

状态：P3 research-only 基础设施。它发现可复现的改进候选，不能自行修改运行时、
提示词、事实层、策略注册表或实时排名。

## 闭环

```text
research_consumer_run_v1
  -> 自动扫描失败、阻塞、重试和证据不足
  -> learning.case.proposed（追加式、内容去重）
  -> 人工检查并补齐冻结 benchmark
  -> learning.case.reviewed
  -> 离线 eval suite
  -> evaluate_agent_harness.py 确定性回放
```

自动扫描只负责提出候选。`accepted` 表示该案例可以进入离线 benchmark，
不是生产审批，也不会触发代码生成、自动 PR、配置修改或策略晋级。

## 数据契约

`learning_case_v1` 绑定：

- 来源 artifact 路径和 SHA-256；
- 失败分类、终态和短 reason code；
- runtime、role、evidence pack 引用和带时区冻结时间；
- `research_only=true` 与 `automatic_effect=none`。

学习账本使用 `learning_event_v1` JSONL，只追加两类事件：

- `learning.case.proposed`；
- `learning.case.reviewed`。

每个事件有独立 hash；损坏、截断、先 review 后 proposal 或重复 proposal 都失败关闭。

## 操作

扫描 research consumer 审计产物：

```bash
python scripts/learning_eval_factory.py scan
python scripts/learning_eval_factory.py status
```

接受案例前，reviewer 必须准备 benchmark JSON，其中包含冻结时间、角色、证据包、
模型返回 fixture、期望终态和期望 reason codes：

```bash
python scripts/learning_eval_factory.py review \
  --case-id lc-... \
  --decision accepted \
  --reviewer '<reviewer>' \
  --benchmark /path/to/reviewed-benchmark.json
```

拒绝不需要 benchmark：

```bash
python scripts/learning_eval_factory.py review \
  --case-id lc-... \
  --decision rejected \
  --reviewer '<reviewer>'
```

物化和回放只写指定的离线目录：

```bash
python scripts/learning_eval_factory.py export --output /tmp/a-stock-learning-eval
python scripts/evaluate_agent_harness.py \
  --cases /tmp/a-stock-learning-eval/cases.json \
  --quiet
```

物化器只导出最新投影状态为 `accepted` 且包含完整 benchmark 的案例。每个 fixture
按内容生成稳定名称；case 可以拥有自己的 `frozen_now`，因此不同日期的历史案例不会被
执行当天墙钟污染。

## 非目标

- 不把一次用户反馈直接传播给所有用户；
- 不自动编辑 prompts、profiles 或代码；
- 不自动创建或合并 PR；
- 不把 eval 通过解释为投资有效；
- 不读取或写入 signal ledger、portfolio 或 cron manifest。

后续 Continuous Learning 自动化只能在此边界外另建“候选变更 → 全量回归 → 人工 PR”
流程，不得把 benchmark acceptance 当成生产授权。
