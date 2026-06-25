---
name: stock-triage
description: >-
  A-share analysis orchestrator. It consumes run-scoped evidence, applies
  quality and portfolio policy, coordinates focused skills, records decisions
  in the signal ledger, and produces auditable recommendations.
version: 1.2.0
author: Luna
metadata:
  hermes:
    tags: [A股, 编排, 风控, 选股, 投资]
    category: finance
---

# A股分析编排器

`stock-triage` 是决策编排层，不是数据源、策略模型或交易执行器。它负责把同一运行批次内
的市场快照、候选证据、持仓状态和研究结论组合起来，经风险 Policy 后写入统一信号账本。

## 运行边界

- 仅提供决策支持，不自动下单。
- 持仓、监控和候选必须从运行时状态读取，不从聊天历史恢复。
- A股股票现货遵守 T+1；当日新增股份不得给出当日卖出动作。
- 数据缺失、过期、来源失败或交易日历未覆盖时，降低结论或阻断方向性建议。
- 所有定时任务由 `cron/hermes-cron-manifest.json` 定义，并通过 `scripts/run_agent_dag.py` 启动。
- Hermes 与 OpenClaw 共用同一状态目录和同一 Signal Ledger。

## 主决策链

```text
版本化市场快照
  -> D0 情绪与涨停梯队
  -> D0 全市场候选发现
  -> D1 集合竞价收敛
  -> D1 开盘确认
  -> 公告/筹码/可成交性/组合风险 Policy
  -> Signal Ledger
  -> T+1/T+3 自动结算
  -> 绩效评估和策略门控
```

入口职责：

| 能力 | 入口 |
| --- | --- |
| 全市场候选发现 | `scripts/candidate_discovery.py` |
| 四维复核 | `scripts/batch_four_dim_scorer.py` |
| 单股四维评分 | `scripts/four_dim_scorer.py` |
| 盘中动态监控 | `scripts/intraday_monitor.py` |
| 资金流缓存 | `scripts/capital_flow_monitor.py --cache` |
| 持仓和 T+1 风控 | `scripts/portfolio_manager.py` |
| 筹码与机构证据 | `scripts/stock_intelligence_refresh.py` |
| 推荐审计 | `scripts/recommendation_audit.py` |
| 自动结算与门控 | `scripts/performance_tracker.py` |
| 动态订阅管理 | `scripts/monitor_manager.py` |

## 动态目标

运行目标统一由 `skills/common/runtime_targets.py` 解析：

1. 当前持仓。
2. 有效的股票、板块和主题订阅。
3. 明确要求的候选池前 N 名。
4. 去重并应用手动取消墓碑。

代码、板块或主题不得写入 Skill 文档、cron command 或默认 prompt。主动取消后，自动候选、
持仓同步和研究队列都不得绕过墓碑重新加入，除非用户明确执行强制恢复。

## 证据与 Policy

方向性建议至少检查：

1. 行情与交易日是否新鲜。
2. 公告扫描是否完成，是否存在澄清或硬风险。
3. 停牌、涨跌停、一字板和报价质量是否允许成交。
4. 解禁、两融、股东户数等必要筹码数据是否完整且未过期。
5. 当前持仓、现金、单票集中度和策略仓位上限。
6. 打板策略的情绪温度与退潮门禁。
7. T+1 可卖日期和隔夜跳空风险。

低置信度或必要证据缺失时只能输出观察条件，不得输出无条件买入。

## Serenity 自动刷新

`skills/common/serenity_refresh_queue.py` 是两套 Agent 运行时共用的确定性队列。目标优先级为：
持仓、有效建议、主动监控、候选池前 5 名。调度器只创建任务，实际研究必须由
`serenity-investment-research` 完成并写入 `deep_research_cache.py`。

```bash
python skills/common/serenity_refresh_queue.py claim --worker hermes
python skills/common/serenity_refresh_queue.py complete --id <request-id>
python skills/common/serenity_refresh_queue.py fail \
  --id <request-id> --error "<reason>"
```

`complete` 会验证缓存存在且研究日期不早于请求日期。没有真实报告时不得完成任务。

## Chanlun 研究信号

`chanlun-backtest/scripts/chan_structure.py` 可以生成分型、笔、中枢、买卖点和背驰证据。
信号必须先由 `research_gate.py --register` 登记为 `allowed_in_live_agent=true` 才能参与
实时权重；未过闸信号只展示为研究假设。

## 输出协议

推荐报告应包含：

- `trading_date`、`batch_id`、证据时间和来源版本。
- 股票代码、策略 ID、信号等级和置信度。
- 入场条件、失效条件、止损、目标、持有周期和仓位上限。
- 公告、筹码、可成交性、组合风险和 T+1 检查结果。
- 被排除维度、降级原因和阻断原因。
- 对应的 Signal Ledger 标识。

无信号的高频任务应静默。被阻断任务只输出阻断原因，不生成伪候选。

## 常用命令

```bash
PYTHONPATH=skills/common \
python skills/stock-triage/scripts/four_dim_scorer.py <code> <name>

PYTHONPATH=skills/common \
python skills/stock-triage/scripts/portfolio_manager.py --check --json

PYTHONPATH=skills/common \
python skills/stock-triage/scripts/monitor_manager.py list --json

python scripts/run_agent_dag.py <job-id> --emit-target
python scripts/validate_cron_manifest.py
```

## 关联技能

- `stock-analyst`：技术、基本面和全市场筛选。
- `hot-money-tactics`：涨停梯队、板块赚钱效应和情绪温度。
- `daban-stock-picker`：竞价、开盘、六问否决和可成交性。
- `policy-intent-decoder`：官方政策意图、传导链和选股辅助维度。
- `news-to-sector`：资讯到板块及股票的影响映射。
- `serenity-investment-research`：深度公司研究。
- `chanlun-backtest`：离线结构研究与策略准入。
- `global-market-monitor`：外围事件到 A 股风险映射。
- `social-sentiment`：多源社会关注度证据。

## 验证

修改编排或调度后至少运行：

```bash
pytest -q
python scripts/validate_cron_manifest.py
git diff --check
```
