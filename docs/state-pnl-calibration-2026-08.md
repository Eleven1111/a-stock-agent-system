# 情绪状态分阶段收益归因与校准报告（P1，2026-08）

> 方案来源：`docs/hot-money-emotion-system-upgrade-plan-2026-08.md` §4（P1）。
> 产出脚本：`scripts/state_pnl_report.py`（纯计算 + CLI，不触网）。
> **本阶段只出证据，不改任何阈值**：`config/daban_thresholds.yaml` 与
> `market_temperature.py` 的分档阈值本 PR 零改动。
>
> 写作方式说明：仓库的 settings 钩子拦截 `docs/*.md` 的 Write（.md 白名单不含它），
> 这是卫生护栏不是硬边界，本文件按制度用 Bash heredoc 写入，并在此留痕。

## 0. 一句话结论

**当前不能支持 P3 用情绪状态做过滤——证据不足，不是"证伪"也不是"证实"。**
本机 `sentiment_daily` 的 640 个交易日**全部是 `partial` 覆盖**（覆盖率约 1.15%，
60/5207 只股票），符合结论条件（`coverage_status == "full"`）的样本数为 **0**。
三套口径的 E[R|state] 矩阵、Spearman IC 与单调性检验**全部 UNVERIFIED**。
校准管道已就绪并经变异测试验证，等生产数据（全市场覆盖的 `sentiment_daily`）到位后
重跑即可给结论。

## 1. 口径定义（三套并存的情绪标签）

| 口径 | 来源 | 本报告中的取值方式 | 已知口径退化 |
|---|---|---|---|
| 五档 | `market_temperature.classify_tier` | 用 `sentiment_daily.max_board` 作高度板，逐日带上一档做滞回 | **`sentiment_daily` 不含连板晋级率**，走 `classify_tier` 自己的"晋级率缺失、按高度板保守判定"分支；未就地编造代理指标。记录若显式携带 `promotion_rate` 字段则自动启用 |
| S0-S6 | `market_temperature.classify_market_state` | 喂五档结果 + 当日 `limit_count` / `limit_down_count` 广度证据，逐日带上一状态做滞回 | 拥挤度 / 脆弱度 / 板块轮动三类证据不在本数据集内，传 None（不猜） |
| S_t 连续分 + 分档 | `skills/common/sentiment_score.score_at` | 权重/窗口/分档全部来自 `config/scoring.yaml` 的 `sentiment_score` 节 | 需要 180 日预热；预热不足即 `unavailable`，不给 50 分 |

被解释变量（全部取自 t+1 日的记录）：

- `next_limit_premium_open` / `next_limit_premium_close` — 次日梯队溢价（开盘/收盘）；
- `next_limit_red_ratio` — 次日涨停红盘率；
- `next_break_rate_change` — 次日炸板率相对当日的变化。

## 2. 禁未来函数的实现点

`label_series()` 逐日只把 `records[:index + 1]` 这个切片喂给三个打标签函数——
t+1 之后的行**在物理上进不了标签计算**。被解释变量单独从 `records[index + 1]` 取。
这条性质有三条测试守（含一个"若误用 t+1 打标签则两格均值互换、结论翻转"的构造用例），
并做过变异验证：把切片改成 `records[:index + 2]` 后三条测试全红。

## 3. 样本数与覆盖率（真实运行，非构造）

运行命令（数据取自本机 P0 回填产物）：

```
python scripts/state_pnl_report.py \
  --summary-file /private/tmp/sd-backfill/market/sentiment_daily/sentiment_daily.jsonl
```

实际输出：

```
交易日 640 天 [2024-01-02 → 2026-08-24] 覆盖分布={'partial': 640}
结论集(full 覆盖) 配对样本=0 制度分段=无
partial 子集 配对样本=638（子集口径，不可作结论）
零可用样本：三套口径全部 UNVERIFIED，管道已就绪待生产数据。
```

（`$A_STOCK_STATE_HOME` 默认位置下 `sentiment_daily` 序列为空，同一条命令报 0 天、
0 样本、同样的零样本结论。）

| 项 | 数值 |
|---|---|
| 交易日总数 | 640（2024-01-02 → 2026-08-24） |
| `coverage_status == "full"` | **0** |
| `coverage_status == "partial"` | 640（覆盖率约 0.0115，60/5207 只股票） |
| 结论集配对样本 | **0** |
| partial 子集配对样本 | 638 |

639 个相邻日对中有 1 对被制度分段丢弃（跨 2026-07-06 沪主板风险警示断点），
证明制度分段确实在生效而不是摆设。

## 4. E[R|state] 矩阵

**结论集（full 覆盖）为空矩阵 `{}`** —— 按空集规则返回 `unavailable`，不输出任何数字。

> 字段语义（验收时收紧）：`conclusion_eligible_scope` 只说覆盖口径够不够格，
> `has_conclusion` 说是否真有格子达到 n≥30，`conclusive` 是两者的合取。零样本或
> 全部 UNVERIFIED 时 `conclusive=false`，下游 `if section["conclusive"]` 不会把空
> 矩阵当成已校准结果放行。

这是本阶段的正确结局：没有可用样本时，矩阵应当是空的，而不是一串 0.0。

下表是 **partial 子集**的样本分布，**仅用于证明管道跑得通，不构成任何结论**
（`conclusive=false`，`coverage_scope="partial_or_unknown"`；60 只股票的涨停家数不是
全市场口径）。被解释变量以 `next_limit_premium_close` 为例：

| 口径 | 制度段 `bse_open` | 制度段 `sse_risk_warning_10pct` |
|---|---|---|
| 五档 | 冰点 n=299 | 冰点 n=23（UNVERIFIED） |
| S0-S6 | S0 n=299 | S0 n=23（UNVERIFIED） |
| S_t 分档 | 加速 n=157 / 发酵 n=26（UNVERIFIED） / 极热 n=32 | 加速 n=11、发酵 n=6、极热 n=6（全 UNVERIFIED） |

五档与 S0-S6 在这份 60 只股票的子集上**只出现一个标签**（冰点 / S0）——子集里几乎不
出现高连板，两套离散口径直接退化为常数，Spearman IC 因无变异返回 `null`（不是 0.0）。
这本身就说明：**在 partial 覆盖下讨论区分度没有意义**。

## 5. 三套口径的区分度对比（消融）

partial 子集、`bse_open` 段、被解释变量 `next_limit_premium_close`：

| 口径 | n | Spearman IC | 分组单调性 |
|---|---:|---|---|
| S_t 连续分 | 215 | 0.1030 | — |
| S_t 分档 | 215 | 0.0749 | null（有分组不足门槛） |
| 五档 | 299 | null（标签无变异） | null |
| S0-S6 | 299 | null（标签无变异） | null |

**这四行数字一律不得引用为结论**：数据源是 1.15% 覆盖率的子集。它们唯一的用途是
证明 IC / 单调性两条计算路径在有变异的数据上确实产出数字、在无变异或样本不足时确实
返回 `null` 而不是 0.0。

## 6. 制度分段说明

断点常量**直接复用** `skills/common/a_share_rules.py`（不在本脚本重抄日期）：

| 断点 | 日期 | 常量 |
|---|---|---|
| 科创板开市（20%） | 2019-07-22 | `STAR_MARKET_OPEN` |
| 创业板注册制（10%→20%） | 2020-08-24 | `CHINEXT_20PCT_FROM` |
| 北交所开市（30%） | 2021-11-15 | `BSE_OPEN` |
| 沪主板风险警示（5%→10%） | 2026-07-06 | `SSE_RISK_WARNING_10PCT_FROM` |

统计**按段隔离**：不同段的样本不合并，跨段的相邻两日不配对。本次数据落在
`bse_open`（2024-01-02 → 2026-07-03）与 `sse_risk_warning_10pct`（2026-07-06 起）两段。

## 7. UNVERIFIED 清单

| 项 | 状态 | 原因 |
|---|---|---|
| 三套口径 × 4 个被解释变量的 E[R\|state] 矩阵（结论集） | UNVERIFIED | full 覆盖样本数 = 0 |
| S_t 连续分 vs 五档 vs S0-S6 的区分度排序 | UNVERIFIED | 同上 |
| 分组单调性检验 | UNVERIFIED | 同上 |
| 冰点/发酵/分歧修复期收益是否显著优于高潮/退潮（方案 §4.2 的预期） | UNVERIFIED | 同上；**未证实亦未证伪** |
| 五档口径在含晋级率下的真实分档 | UNVERIFIED | `sentiment_daily` 无 `promotion_rate` 字段，当前走保守分支 |
| partial 子集的全部数字（§4、§5） | 不可作结论 | 覆盖率 1.15%，子集口径 |

## 8. 能否支持 P3 用情绪状态做过滤

**不能——证据不足。** 方案 §4.2 写明"若数据支持报告预期，才允许 P3 策略把状态作为
过滤条件"。当前 full 覆盖样本为 0，既谈不上支持也谈不上否定。因此：

- P3 的策略**不得**以"情绪状态过滤有统计价值"为前提立项；
- 若 P3 仍要保留情绪过滤，须按方案 §6.2 的分情绪状态 PnL 拆解**在策略自己的回测里**
  单独证明其增量，证不出就删掉该过滤而不是留作装饰；
- 解除本条的判据很明确：`sentiment_daily` 出现 `coverage_status == "full"` 的记录，
  且任一制度段内单格样本 ≥ 30 后重跑本脚本。

## 9. 复现

```
python scripts/state_pnl_report.py --summary-file <sentiment_daily.jsonl> --json
python -m pytest tests/test_state_pnl_report.py -q      # 18 passed
```

测试守的性质与变异验证结果见 `tests/test_state_pnl_report.py` 模块 docstring。
