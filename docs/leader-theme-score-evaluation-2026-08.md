# LeaderScore / ThemeScore 评估报告（升级方案 P2，2026-08-25）

> 结论先行：**管道就绪，效果未验证。** 本机零可用真实样本（sentiment_daily 0 行、
> theme_registry 0 个题材、无落盘的 hot_money_selection 状态），因此本报告
> **不含任何区分度或预测力结论**。两个新分一律 shadow，不得替换现有排序或 4 因子分。

## 1. 因子定义与权重表

权重与阈值全部在 `config/scoring.yaml` 的 `leader_score` / `theme_score` 节；两个模块内
**没有**等价的数字副本，配置缺失即整体 `unavailable`（两份数字并存迟早分叉）。

### LeaderScore（`skills/common/hot_money_selection.py`，0-100）

| 因子 | 权重 | 定义 | 数据源 |
|---|---:|---|---|
| H 高度 | 0.25 | 自身连板高度 / 全场最高板，上限 1.0 | `lianban_ladder` → `board_height` / `market_space_height` |
| F 封速 | 0.20 | 1 − 首封距 09:30 的分钟数 / 60（**仅深度池内**） | 深度池分钟线 `first_seal` |
| R 分歧承接 | 0.15 | 开板次数与回封耗时两个子分量的均值（**仅深度池内**） | `open_count` / `reseal_minutes` |
| B 助攻广度 | 0.15 | （板块涨停数 − 自己）/ 5，上限 1.0 | `sectors[].limitup_count` |
| RS 相对强度 | 0.15 | 相对全市场中位与板块前十均值超额的对称映射（0.5 = 持平） | 候选 `change_pct` + `top10_change` |
| A 关注度 | 0.10 | `attention_score` / 100，回退到所属主题关注分 | `social_attention` |

合计 1.00；`min_available_weight = 0.60`。

### ThemeScore（`skills/common/theme_strength.py`，0-100）

`ThemeScore = 0.35·N + 0.30·T + 0.35·B`，`min_available_weight = 0.65`。

- **N 新鲜度** = 0.6 × `0.5 ** (注册天数 / 5)` + 0.4 × 当日新闻 novelty 比例。
  新闻键的口径直接复用 `novelty_gate.content_key`（纯函数，无缓存 IO），不另造归一化。
  两个子分量各自可单独降级并在 N 内部重归一化；两个都缺 → N `unavailable`。
- **T 时机** = S_t 分档分（冰点 0.5 / 修复 1.0 / 发酵 0.8 / 加速 0.4 / 极热 0.1）
  + ΔS>0 时 +0.1，clip 到 [0,1]。消费 P0 已合入的 `sentiment_score.compute_sentiment_score`。
- **B 广度** = 0.5 × min(1, 涨停家数/5) + 0.5 × 上涨占比，直接复用既有 `compute_breadth`。

**所有数值都是方案 §5.1 的待检验初始参数，未经任何历史收益校准**；两个分的输出恒带
`calibrated=false` / `shadow_only=true`。

## 2. 降级规则（fail-closed）

1. 任一因子/维度数据缺失 → 该项 `unavailable`，其权重**移出分母重新归一化**。
   绝不用 0 或中位数冒充观测值 —— 否则深度池外的标的会被系统性低估。
2. 可用权重低于 `min_available_weight`，或全部因子缺失 → 整体 `unavailable`，
   **不返回 0 分**（0 分是一个观测结论，缺数据不是）。
3. 深度池外：F(0.20) + R(0.15) 必然缺失，余 0.65 仍可出分；再缺一维即整体不可用。
4. 后排"大面"证据不可判定（最近一日既无炸板率也无龙头受损）→ `back_row_damage_days = None`，
   孤板惩罚**不触发**：不伪造惩罚，也不伪造豁免。

## 3. "最高板 ≠ 龙头" 的显式编码

现有排序以连板高度为第一键，等于把"最高板"当成了龙头的同义词。研究手册的口径相反：
最高板只是六项之一，高位孤板在后排连续大面时是"最后的强势"，不是安全买点。

判据三条**同时**成立才算高位孤板（`_isolation_state`）：
自身即全场最高板 ∧ 板块内除自己外涨停数 ≤ 1 ∧ 后排连续大面 ≥ 2 日
（中位板炸板率 ≥ 0.5 或龙头次日收益 ≤ −5%）。

触发时 **B、R 强制为 0.0 而不是 `unavailable`** —— 孤板且后排连续大面本身就是对
"没有助攻、承接无从谈起"的直接观测，权重必须留在分母里让分数掉下来。这一条优先于
深度池降级：它不需要分钟线也成立。

## 4. shadow 隔离证据

- 新分写入**另一个字段** `leader_score_shadow`；既有的 `leader_score`
  （= 100 − (rank−1)×15 的排名代理）、`leader_role`、`leader_rank`、
  `hot_money_qualified` 与列表顺序原样透传。
- `apply_leader_score_shadow` 返回候选副本，不改输入；模块内除该函数外没有第二个读点。
- ThemeScore 不写入 `sectors[]`，既有 4 因子权重
  （涨停 0.45 / 成交额 0.20 / 前十涨幅 0.25 / 关注度 0.10）逐字段未动。
- **行为断言**（`tests/test_leader_theme_score.py`）：
  - `test_shadow_scoring_leaves_ranking_identity_and_gate_untouched` —— 同一输入下，
    加分前后每个候选逐字段一致（新增键集合恰为 `{leader_score_shadow}`），顺序一致，
    `selection_strategy_id` 与 `selection_context_for` 输出逐字段一致；
  - `test_extreme_shadow_score_does_not_change_gate_or_order` —— 把 shadow 分推到两端
    （全池内/极低基准 vs 全池外/孤板大面），`leader_rank` / `leader_role` /
    `hot_money_qualified` / `hot_money_gate_reasons` 全部不变；
  - `test_existing_four_factor_sector_score_is_not_replaced` —— 既有板块分与权重不变。

## 5. 真实样本数：**0**（UNVERIFIED）

2026-08-25 对本机 state home `/Users/na/.hermes` 实跑（脚本见交付报告附录）：

| 输入 | 实测 |
|---|---|
| `sentiment_daily` 汇总行数 | **0** |
| S_t 状态 | `unavailable` / `empty_series` |
| `theme_registry` 题材数 | **0** |
| ThemeScore 实跑样本 | **0**（其中 status=ok：0） |
| `hot_money_selection_latest.json` | **不存在** |
| LeaderScore 实跑样本 | **0**（其中 status=ok：0） |
| Top2 分歧案例 | 无法统计 |

生产数据在部署机，不在本机（见 MEMORY 生产部署拓扑）。因此本轮**没有**、也不可能有
任何区分度/单调性/预测力结论。合成 fixture 只证明算法按定义算对了，不证明它有用。

## 6. UNVERIFIED 清单

- [ ] LeaderScore 与现排序 Top2 的一致率 ≥ 20 个交易日统计（方案 §5.2-1）：**零样本，未启动**。
- [ ] 不一致案例逐个人工复核：**零案例**。
- [ ] LeaderScore 分组对次日收益的单调性检验（方案 §5.2-2）：**零样本，未启动**。
      分组非空断言必须先于结论 —— 空集恒成立不是通过。
- [ ] F/R 因子在真实深度池数据上的可得率：**未观测**（本机无分钟线缓存）。
- [ ] ThemeScore 对"主线板块持续 ≥ 2 日"的预测力 vs 现 4 因子分对比（方案 §5.2-4）：
      **零样本，未启动**。
- [ ] T 维的分档分（冰点 0.5 / 修复 1.0 / …）与 N 维的半衰期 5 日：
      **纯先验，从未用历史收益校准**。
- [ ] 孤板惩罚的三条阈值（助攻 ≤1、大面 ≥2 日、炸板率 ≥0.5、龙头受损 ≤−5%）：**纯先验**。
- [ ] `append_leader_score_divergences` 的落盘路径在生产 DAG 中尚**未接线**——
      本轮只提供函数，没有调用方。

## 7. 结论：当前能否替换现有排序 / 4 因子分

**不能，证据不足。** 替换需要方案 §5.2 的四条验收全部落地，而它们无一例外都要求真实样本；
本机零样本，一条都没跑成。当前状态只是"管道就绪"：算法按定义算对了（合成 fixture +
7 项变异测试），降级与隔离守得住，但**它有没有用完全未知**。

下一步的判据是数据而不是代码：`sentiment_daily` 出现 ≥ `min_history`(180) 行、
`theme_registry` 有活跃题材、生产选股状态可回传本机后，再启动 §5.2 的四项验收。
在那之前，任何"LeaderScore 更准"的说法都没有证据支撑。
