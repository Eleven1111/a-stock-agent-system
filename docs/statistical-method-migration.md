# 统计方法版本迁移：`statistical-validation-suite-v1` → `v2`

生效日期：2026-09-05。代码基线 `bbb102c`。触发原因见下「被修正的缺陷」。

## 被修正的缺陷

### 1. Deflated Sharpe 的阈值缺了跨试验离散度

`deflated_sharpe` 按 Bailey & López de Prado (2014) 应当计算

```
E[max SR] = sqrt(V) · [ (1-γ)·Z⁻¹(1 − 1/N) + γ·Z⁻¹(1 − 1/(N·e)) ]
```

其中 `V` 是**同口径试验集合**里各试验 Sharpe 的方差，`γ` 是 Euler–Mascheroni 常数。

v1 实现写的是 `Z⁻¹(1 − 1/(N+1))`——既用错了极值分布的位置项，又整体丢掉了 `sqrt(V)`
这个尺度因子。后果是把一个**逐期** Sharpe 拿去和一个标准正态分位数比较：

```
# v1，10000 个日频收益、trials=6
sharpe = 0.1181, expected_maximum_sharpe = 1.0676, probability = 0.0
```

0.1181 与 1.0676 根本不在同一个量纲上，任何策略都必然 `probability = 0`，
`deflated_sharpe_within_limit` 这道门因此**恒假**——它不是一道严格的门，是一道坏掉的门。

v2 的处理：
- 试验集合由调用链提供（`observed_trial_sharpes` 从 `variant_returns` 计算，同频率）。
- 拿不到离散度时返回 `not_evaluated / trial_dispersion_unavailable`，**不补一个 variance=1**。
- 离散度为零（各试验 Sharpe 相同）返回 `not_evaluated / trial_dispersion_degenerate`。
- `trials == 1` 时退化为 probabilistic Sharpe ratio（`method` 改为
  `probabilistic_sharpe_ratio`，`expected_maximum_sharpe = 0`），语义显式标出。
- 产物新增 `method_version` / `effective_trials` / `trial_sharpe_variance` /
  `threshold_source` / `observation_count` / `return_frequency`。

### 2. CSCV 把无法区分的变体当成互相竞争的试验

v1 对两条**完全相同**的序列返回 `pbo = 0.0`（"没有过拟合"）。k 份同一策略的拷贝是
一个试验，不是 k 个。v2 先按序列内容折叠，折叠后不足两个可区分变体时返回
`not_evaluated / insufficient_distinct_variants`，并在 `duplicate_groups` 里披露折叠关系。

### 3. CSCV 用变体名字做经济排名的 tie-break

v1 的 `max(train_means, key=lambda k: (train_means[k], k))` 和
`sorted((mean, key))` 都让字典序参与了排名：同一份数据换个策略名字就可能换个结论。
v2 改为并列共享中位秩、并列选中集合上对结果取平均，结论对命名与插入顺序不变。

### 4. 有效样本量把「广度」讲成了「独立性」

`compute_effective_samples` 的 `trade` 是 (session, stock) 上的 Kish 广度：同一天开
30 只票 → `trade = 30`，读起来像 30 个独立样本，实际只有一天的市场信息。
v2 保留全部旧字段与旧阈值（**不动准入标准**），新增 `session` / `distinct_sessions` /
`basis: "kish_breadth"` / `autocorrelation_adjusted: false`，并让缺失的 sector 维度
显式为 `sector: null, sector_status: "unavailable"`，防止「没做 sector 聚类」
被读成「sector 聚类已满足」。

## 未改的部分（刻意）

- `config/validation_thresholds.json` 一字未动。修的是估计量，不是门槛。
  DSR 那道门从「恒假」变成「真的会判」之后，`minimum_deflated_sharpe_probability: 0.95`
  这个阈值本身是否仍然合适，**需要一次独立研究决定**，不在本轮范围内。
- FDR 的 p 值已经绑定原始收益序列（`tail_close_validation._normal_mean_p_value`
  从 `variant_returns` 现算），复查后无需修改。
- `minimum_regime_effective_samples: 3` 的普适性问题（单状态策略被逼成全天候策略）
  仍未解决，见下「待办」。
- purge/embargo：`probability_of_backtest_overfitting` 新增可选 `embargo` 参数并在
  `purge_embargo` 字段披露是否启用。**默认 0 且披露 `applied: false`**——当前输入
  没有携带持仓窗口/标签跨度信息，声称已处理重叠会是假话。

## 需要重算的历史结论

| 产物 | 处置 |
|---|---|
| 任何 `schema_version == "statistical-validation-suite-v1"` 的统计产物 | **只读保留，不再通过 `verify_validation_artifact`**。需要用 v2 重算才能重新支撑准入。 |
| 依赖 `deflated_sharpe_within_limit` 判定过的晋级 | 该检查在 v1 下恒假，因此不存在「靠它通过」的历史批准；但也不存在「它拦住过什么」的证据。 |
| 依赖 `pbo_within_limit` 判定过的晋级 | 若当时变体集合含重复列，v1 的 `pbo = 0` 无决策意义，须重算。 |

扫描结果（2026-09-05，`grep -rl "statistical-validation-suite-v1"` 排除 .git）：
仓库内**没有**已落盘的 v1 统计产物文件，只有代码与测试引用。部署机上的产物
未核查（本机无 openclaw、无部署访问）——状态 `deployment_unverified`，
重算需在部署机上按上表执行。

**不回改历史 hash，不批量重新批准。** 旧记录保持原样只读。

## 待办（本轮明确不做）

- `minimum_deflated_sharpe_probability` 阈值在 v2 语义下的重新标定。
- `minimum_regime_effective_samples` 对单状态策略的适用域声明（域内样本门 / 域外不触发）。
- CSCV 的 purge/embargo 需要输入携带 `session` + `horizon` 才能真正生效，
  当前只提供了参数与披露位。
