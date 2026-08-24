# checkpoint: 板块强度盘中时序落盘

方案: `.omx/plans/sector-intraday-series.md`
上游背景: 宿主机对 agent 提出 6 条改进建议；核实后 4 条能力已存在，真缺口为
"盘中时序未落盘"（第3条）与"回测样本量未知"（第6条）。本轮做这两项。

前序: issue #260（市场/板块门禁拆分，宿主机建议第5条）已由 PR #261 合并。

## 本轮状态

- [x] 现状核实 —— 6 条建议逐条对照代码，结论见下方"核实结论"
- [x] 方案 —— `.omx/plans/sector-intraday-series.md`
- [x] 实现
  - [x] 新增 `skills/common/sector_series.py`（纯函数）：`slot_of` 15分钟分桶、
        `record_slot` 幂等 upsert、`derive_persistence` 消费方、`summarize_day`
        有界摘要、`prune_old_days` 保留 20 日
  - [x] `intraday_monitor.py` 接线：新增 `_record_sector_series()`，落盘失败
        fail-open（不压制涨跌停/退出告警）但把失败显式带回返回值
  - [x] cron manifest 白名单加 `sector_series/`（**不加会导致生产写入被拦**）
  - [x] 部署机只读诊断 `scripts/diagnose_settlement_samples.py`
- [x] 测试 —— `tests/test_sector_series.py` 25 个 + `test_intraday_monitor.py` +3
- [x] Mutation check（4 项全部确认变红后复原）
- [x] 验证门槛 —— 见下

## 三条 fail-closed 语义（本轮核心）

1. **"没跑" ≠ "跑了但没数据"**：`slots` 记录所有执行过的槽位，
   `degraded_slots` 记录执行了但观测不可用的槽位。缺 slot = 运维问题，
   两者都有 = 数据问题。（issue #112/#113 教训）
2. **缺口不插值**：斜率委托 `market_temperature.three_day_slope`，
   不足 3 点返回 None，不用 0 冒充"走平"。
3. **空集不出恒真数**：`derive_persistence({})` 返回 `insufficient_slots`，
   不返回比率。降级槽计入分母，否则持续性被系统性高估。

## Mutation check 结果（testing.md 要求）

| 变异 | 预期 | 实测 |
|---|---|---|
| 去掉同槽幂等（改无条件追加） | 幂等测试红 | ✅ 2 个红 |
| 去掉 degraded 记录 | degraded 测试红 | ✅ 1 个红 |
| 斜率不足3点时用 0.0 冒充 | 缺口测试红 | ✅ 1 个红 |
| 降级槽从分母消失 | 比率测试红 | ✅ 1 个红（1.0 vs 0.75，正是"覆盖率恒1.0"类假绿） |

## 验证门槛（本地 3.13）

全量 pytest **3084 passed**（+3 既有环境失败，与本次无关，已用 git stash 核实历史）
/ ruff 全绿 / compileall 全绿 / validate_cron_manifest OK(67 jobs)
/ maintainability budget 全绿 / git diff --check 全绿。
**CI 3.10 矩阵待远端跑。**

## 核实结论：宿主机 6 条建议 vs 代码现状

| # | 建议 | 现状 | 真缺口 |
|---|---|---|---|
| 1 | 板块/主题映射+角色 | `industry_map.py`(815行)、`theme_registry.py` 支持三类证据、角色 leader/core/follower | 板块**扁平**（`resolve_sector` 单值），做不了"贵金属→白银→资源扩散"层级；缺"补涨"档 |
| 2 | 板块强度指标 | 中位数/上涨数/涨停数/相对大盘/核心股表现均有 | 缺 3 个小指标：板块级炸板率、5%档计数、成交额增速 |
| 3 | 盘中时序 | 时点基础设施已有 | **本轮已修复** |
| 4 | 板块内部结构 | 零件齐（消融/拥挤/轮动/S0-S6） | 缺"低位补涨"与板块级"诱多回落"；主要缺聚合层 |
| 5 | 市场/板块门禁拆分 | #260 已合并 | 只做了冰点→restricted 一个方向；反方向（强势市≠全板块可参与）未做 |
| 6 | 回测校准 | 机器齐（`lifecycle_analytics` 有 IC/消融/分阶段） | **数据未知** —— 本轮交付诊断脚本待部署机上跑 |

## 下一步（建议顺序）

1. **部署机跑 `python scripts/diagnose_settlement_samples.py`** —— 拿回结算样本量，
   决定第6条是"接线"还是"重建"。这是所有校准的前提。
2. 第2条缺的 3 个指标（炸板率/5%档/成交额增速）—— 小增量
3. 第4条形态聚合层 —— 纯聚合，不新增数据源
4. 第1条层级化 —— 最贵，要动 `resolve_sector` 契约及全部下游，建议单独立项

## 明确未做

- 板块强度摘要**未接入简报** —— 刻意的：先确认时序数据可靠再决定呈现
- 不做主线持续性**判断**（只落盘+出摘要，不出"是否主线"结论）
- 不改告警阈值、不动 #260 门禁语义、不新增 cron 作业
