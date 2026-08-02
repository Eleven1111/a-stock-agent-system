# chanlun T6 门控重评报告（2026-08-02，结构升级后 · v2 谱系）

对应 `docs/chanlun-upgrade-plan-2026-08.md` 任务 T6：结构层升级（T1–T4，与 chan.py
差分 100% 对齐）后，用**版本化 strategy_id + 新留出集**重跑门控评估，回答"结构保真
度提升是否改变 `docs/chanlun-gate-evaluation-2026-07.md` 的 verdict B 结论"。

**结论先行：verdict 仍为 B——结构升级后的 12 个全谱系买卖点信号
（`chanlun_bsp{1,1p,2,2s,3a,3b}_{buy,sell}_v2`）在新留出集（2025-08-01 起 OOS）上
全部未通过门控（`failed` 或 `blocked`），`allowed_in_live_agent` 全部为 `false`。
结构保真度提升没有改变定位结论：chanlun 继续作为结构位置过滤器，不作为独立信号源
参与打分/排序。** 本任务未调用 `--register`，`strategy_registry` 零改动（见 §6 验证）。

---

## 1. 评估对象

结构层 T1–T4 升级（笔虚笔/is_sure、线段 EigenFX、段内中枢、买卖点全谱系 T1/T1P/T2/
T2S/T3A/T3B + 背驰算法族）落地后，`chan_structure.analyze()` 的 `signals` 新增
`bsp_type`/`is_buy`/`is_sure`/`feature_dict`/`strategy_id_v2` 五个字段（旧字段与旧四
类型 `strategy_id` 全部保留，legacy 评估协议不受影响）。本任务评估 `strategy_id_v2`
覆盖的全部 12 个假设：

| bsp_type | 缠论形态 | buy strategy_id_v2 | sell strategy_id_v2 |
|---|---|---|---|
| 1 | 一类背驰（趋势） | `chanlun_bsp1_buy_v2` | `chanlun_bsp1_sell_v2` |
| 1p | 盘整背驰 | `chanlun_bsp1p_buy_v2` | `chanlun_bsp1p_sell_v2` |
| 2 | 二类（回抽不破） | `chanlun_bsp2_buy_v2` | `chanlun_bsp2_sell_v2` |
| 2s | 类二 | `chanlun_bsp2s_buy_v2` | `chanlun_bsp2s_sell_v2` |
| 3a | 三类（中枢后） | `chanlun_bsp3a_buy_v2` | `chanlun_bsp3a_sell_v2` |
| 3b | 三类（中枢前） | `chanlun_bsp3b_buy_v2` | `chanlun_bsp3b_sell_v2` |

只统计**锚定笔 `is_sure=True`**（已确认/非虚笔）的信号——虚笔端点会随新K线延伸或撤销，
把它们计入"信号首次可观察时点"会污染无前视偏差假设，故在 `extract_signal_events`
（`strategy_id_field="strategy_id_v2"`，`require_is_sure=True`）层面过滤。

入场规则沿用上轮不变（`entry_rule = first_detection_then_next_bar_open`）：信号在历
史前缀上首次可观察后的下一交易日开盘价入场，T+1 计净收益（已扣佣金/滑点/印花税），
方向标准化（看空信号收益 = -gross，作规避信号统计，非宣称做空）。

legacy 四类型（`chanlun_third_buy`/`chanlun_third_sell`/`chanlun_bottom_divergence`/
`chanlun_top_divergence`）的既有代码路径、已登记 OOS 台账（`chanlun_four_signal_oos`）、
证据产物目录（`evidence/gate_evaluation/`）**本任务零改动**——`analyze_payload(...,
lineage="legacy")` 仍产出与 2026-07-03 完全一致的四 ID 协议，只是现在也走升级后的
`chan_structure.analyze()`（旧字段契约不变，见 §5 差分验证）。

## 2. 数据范围（真实运行）

- **数据源**：mootdx（通达信 TCP 直连）。本次评估会话中默认 bestip 缓存
  （`~/.mootdx/config.json`）指向的服务器对 `get_security_bars` 静默返回 0 行（`stocks()`
  列表接口可用但 K 线接口不可用，属本机 mootdx 本地缓存的过期最佳节点，非代码/网络问题）；
  改用实测可用节点（`180.153.18.170:7709`）后取数正常。此为本机环境修复，不改动仓库代码
  或 `mootdx_source.py` 默认行为，部署机若遇到同类"能连但取不到K线"的情况，可参照同一
  排查路径（`client.stocks()` 通但 `client.bars()` 空 → 换 bestip 节点）。
- **标的**：与上轮完全一致的 20 只固定 universe（贵州茅台/平安银行/招商银行/格力电器/
  中国平安/宁德时代/海康威视/中信证券/美的集团/紫金矿业/浦发银行/万科A/兴业银行/恒瑞
  医药/五粮液/中国中免/山西汾酒/京东方A/隆基绿能/东方财富）。20 只全部成功拉取，
  `skipped_short_history: []`。
- **基准**：沪深300指数（000300），`mootdx_source.fetch_index_daily()`（`index_bars()`）。
- **时间范围**：2016-01-20 ~ 2026-07-31（`--start 2019-11-01`，mootdx 分页实际回溯到
  2016-01；20 只个股 + 基准共 47,576 根日K，深于上轮请求窗口，未剔除更早历史）。
- **IS/OOS 切分日**：**2025-08-01**（新留出集，IS ~5.8 年，OOS ~1 年到数据末端
  2026-07-31），与上轮 2025-07-01 切分不同——避免复用同一留出集重复检验同一批数据。
- **事件样本**：12 个假设合计 2,390 个事件（`sample.events`），按 `strategy_id_v2`
  分桶后的 IS/OOS 计数见 §3。
- **对照组**：`random_entry`（同池 20% 稳定哈希采样）、`simple_breakout`（20 日收盘突
  破/破位）、`buy_hold`（沪深300 逐日开-收基准，方向标准化）——与上轮完全一致，三组均
  为真实样本（无 `missing` 阻断项）。
- **统计检验**：单样本 t 检验、bootstrap 置信区间（2000 次重采样）、置换检验（**5000
  次**，signal vs 最强对照组）、Benjamini-Hochberg FDR 校正（**q=0.10，12 个假设一起
  校正**——比上轮 4 个假设的校正基数更大，同等 p 值下更难通过 FDR 门槛）。
  `min_oos_samples=30`（与上轮一致）。
- **协议指纹**（写入证据产物与 `research_protocol`，供复跑校验；与 legacy 协议指纹不同，
  版本化隔离生效）：
  - `lineage`: `v2`
  - `rules_version`: `chan-structural-v2-t1t4-bsp-lineage`
  - `rules_fingerprint`: `bbbd013716621a63d72769811fd5c8cd7667ae9e6df4579f46582e4f8d6ad183`
  - `dataset_fingerprint`: `ae8cb67bf8588a4b3361cd306a4ec325892df517d64d49a52c1eee8bbd9ea2d6`
- **运行耗时**：单核 4 分 12 秒（`--permutations 5000`，12 个假设 × t1/t3 两变体 ×
  IS/OOS 两期，`time` 实测 `247.96s user`）。

## 3. IS/OOS 结果与统计检验

主检验字段为 T+1 净收益（`t1_return`，已扣成本，方向标准化）。

| strategy_id_v2 | 方向 | 样本(IS/OOS) | permutation_p | FDR-adjusted p (q=0.10, n=12) | oos_alpha (signal-control) | gate 判定 | 阻断原因 |
|---|---|---|---|---|---|---|---|
| `chanlun_bsp1_buy_v2` | bullish | 279/65 | 0.0164 | 0.0984 | **-0.00511** | `failed` | 无（样本充足但方向为负） |
| `chanlun_bsp1_sell_v2` | bearish | 317/34 | 0.3659 | 0.4879 | +0.00430 | `failed` | 无（不显著） |
| `chanlun_bsp1p_buy_v2` | bullish | 121/25 | 0.1982 | 0.2972 | +0.00421 | `blocked` | 样本量不足: 25<30 |
| `chanlun_bsp1p_sell_v2` | bearish | 135/7 | 0.0946 | 0.1892 | -0.01614 | `blocked` | 样本量不足: 7<30 |
| `chanlun_bsp2_buy_v2` | bullish | 249/26 | 0.0450 | 0.1800 | **-0.00594** | `blocked` | 样本量不足: 26<30 |
| `chanlun_bsp2_sell_v2` | bearish | 266/19 | 0.1430 | 0.2451 | +0.00876 | `blocked` | 样本量不足: 19<30 |
| `chanlun_bsp2s_buy_v2` | bullish | 305/31 | 0.0898 | 0.1892 | -0.00462 | `failed` | 无（不显著） |
| `chanlun_bsp2s_sell_v2` | bearish | 272/27 | 0.6645 | 0.6645 | -0.00212 | `blocked` | 样本量不足: 27<30 |
| `chanlun_bsp3a_buy_v2` | bullish | 67/10 | 0.0780 | 0.1892 | +0.00819 | `blocked` | 样本量不足: 10<30 |
| `chanlun_bsp3a_sell_v2` | bearish | 41/8 | 0.4883 | 0.5327 | -0.00612 | `blocked` | 样本量不足: 8<30 |
| `chanlun_bsp3b_buy_v2` | bullish | 49/2 | 0.0124 | 0.0984 | **-0.02581** | `blocked` | 样本量不足: 2<30 |
| `chanlun_bsp3b_sell_v2` | bearish | 33/2 | 0.4421 | 0.5305 | +0.01111 | `blocked` | 样本量不足: 2<30 |

逐条解读：

- **样本量充足、可下结论的 4 个 ID**（`bsp1_buy/sell_v2`、`bsp2s_buy_v2`，OOS 均
  ≥30，另 `bsp2s_sell_v2` OOS=27 接近门槛但仍 `blocked`）：`bsp1_buy_v2` OOS
  permutation_p=0.0164 看似"显著"，FDR 校正后 0.0984（q=0.10 边缘），但 `oos_alpha`
  为 **-0.00511**——方向是反的，"显著地跑输最强对照"，`research_gate` 正确判定
  `failed`。其余三个（`bsp1_sell_v2`/`bsp2s_buy_v2`/`bsp2s_sell_v2`）permutation_p
  均不显著（0.09～0.66）。一类背驰（趋势背驰，理论上缠论买卖点谱系里"最正宗"的一类）
  升级后依然没有正向 edge，与上轮 `chanlun_bottom_divergence`/`chanlun_top_divergence`
  两个背驰信号"样本充足但不显著"的结论一致。
- **样本不足、无法下结论的 8 个 ID**：`bsp1p_*`/`bsp2_*`/`bsp3a_*`/`bsp3b_*` 的 OOS 事件
  数在 2～26 之间，均低于 `min_oos_samples=30`，`research_gate` 判定 `blocked`。其中
  `bsp2_buy_v2`（26/30，点估计 `oos_alpha`=-0.00594）和 `bsp3b_buy_v2`（2/30，
  permutation_p=0.0124 但样本仅 2 个、`oos_alpha`=-0.02581）即使把点估计当真，方向也
  不支持"新谱系比旧四类型更有 edge"的假设。三类买卖点分拆为 3a/3b 两个变体后，单变体
  的触发频率进一步稀释（上轮合并统计的 `chanlun_third_buy` OOS=11，本轮 `bsp3a_buy_v2`
  +`bsp3b_buy_v2` 合计 OOS=12，触发总量相近，只是谱系拆分导致单 ID 样本更薄）。
- **12 个假设一起做 FDR 校正**（Benjamini-Hochberg，q=0.10）后，没有一个 ID 的
  adjusted p 落在"显著且方向为正"的拒绝域——`bsp1_buy_v2` 和 `bsp3b_buy_v2` 的
  adjusted p（0.0984）虽低于 0.10，但两者 `oos_alpha` 均为负，`research_gate` 的
  `permutation_p<=0.05 and fdr_p<=0.10 and oos_alpha>max(0,benchmark_alpha)` 三条件联
  合判据里第三条不满足，仍判 `failed`/`blocked`，不会误放行。

## 4. 门控判定汇总

| strategy_id_v2 | `research_gate.evaluate_gate` decision | `allowed_in_live_agent` |
|---|---|---|
| chanlun_bsp1_buy_v2 | failed | false |
| chanlun_bsp1_sell_v2 | failed | false |
| chanlun_bsp1p_buy_v2 | blocked | false |
| chanlun_bsp1p_sell_v2 | blocked | false |
| chanlun_bsp2_buy_v2 | blocked | false |
| chanlun_bsp2_sell_v2 | blocked | false |
| chanlun_bsp2s_buy_v2 | failed | false |
| chanlun_bsp2s_sell_v2 | blocked | false |
| chanlun_bsp3a_buy_v2 | blocked | false |
| chanlun_bsp3a_sell_v2 | blocked | false |
| chanlun_bsp3b_buy_v2 | blocked | false |
| chanlun_bsp3b_sell_v2 | blocked | false |

12 个 ID 全部未通过门控（`failed` 或 `blocked`，均不等于 `passed_for_reference`）。
`allowed_in_live_agent` 全部为 `false`。本次评估**没有**调用 `--register`——
`grep -rn -- "--register"` 命中的三处（`chan_signal_backtest.py` 的 argparse 定义
及其内部校验，`chanlun_gate_evaluation.py` 里则完全不存在该开关）均只是 CLI 参数
定义，实际执行命令自始至终只用了 `--mode real --lineage v2 --split ...
--permutations 5000 --json`，未加 `--register`——`strategy_registry.json` 在本次
评估前后均不存在
（`find ~/.hermes -iname "*strategy_registry*"` 只命中一个空 `.lock` 占位文件，
mtime 早于本次改动），符合任务硬约束。

## 5. 定位建议

**verdict: B（结构位置过滤器）—— 与 2026-07-03 结论一致，未被结构升级推翻**

结构保真度提升（虚笔/is_sure、线段 EigenFX、段内中枢、买卖点全谱系）本身是有效的
（T0–T4 每步都有 fresh reviewer 验收 + 与 chan.py 差分 100% 对齐，见
`docs/chanlun-upgrade-plan-2026-08.md` 附属 commit），但结构保真度不等于统计 edge：
样本充足的 4 个 ID（尤其一类背驰 `bsp1_*`，理论上最贴近"趋势反转"的经典形态）依然
没有正向、显著的 OOS alpha；样本不足的 8 个 ID 无法下结论，即使把点估计当真，方向也
不稳定为正。这与升级方案 `docs/chanlun-upgrade-plan-2026-08.md` §5 的风险预判一致：
"结构保真度提高 ≠ 出现 edge……T6 完全可能再次给出 verdict B——那也是有效结论"。

后续动作（按既定路线，不在本任务范围内执行）：

1. chanlun（legacy 四类型 + v2 全谱系）继续保持"结构位置过滤器"定位，不作为独立信号
   源参与打分/排序；
2. T5 已完成的 `structure_position` 证据包接入（`research_evidence.py` /
   `research_synthesis.py` risk_redteam 引用）保持不变，继续供 risk_flag 使用；
3. 三类买卖点（3a/3b）触发频率偏低是本轮新增的观察——若未来仍想验证三类买卖点假设，
   需要更长历史窗口或更大 universe 才可能积累到 `min_oos_samples`，而不是继续拆细
   谱系；
4. 不建议在当前证据下扩展缠论信号化投入；`structure_position` 证据服务定位已经是
   本模块能提供的最大价值。

本报告本身不做任何注册/集成变更，只产出评估证据。

## 6. 代码改动

最小改动，复用现有评估框架（成本模型/对照组/统计检验/证据落盘不重写）：

- `skills/chanlun-backtest/scripts/chan_signal_backtest.py`：
  - 新增 `STRATEGY_DIRECTIONS_V2`（12 个 `chanlun_bsp{1,1p,2,2s,3a,3b}_{buy,sell}_v2`
    → bullish/bearish）与 `RULES_VERSION_V2`；legacy `STRATEGY_DIRECTIONS`/
    `RULES_VERSION` 零改动。
  - `extract_signal_events`/`build_control_pools`/`analyze_events` 新增
    `strategy_directions`/`strategy_id_field`/`require_is_sure` 可选参数，默认值
    与原实现完全一致（未传参时行为 100% 不变，legacy 调用路径与已登记 OOS 台账不受
    影响）。
  - `analyze_payload` 新增 `lineage: "legacy" | "v2"` 参数，拆出 `_lineage_config()`
    / `_research_protocol()` 两个辅助函数（避免主函数超过 80 行触发
    `check_maintainability_budget.py` 的 `long_function` 预算）；v2 谱系用独立
    `rules_version` 计算出与 legacy 不同的 `rules_fingerprint`，物理上不可能被误认
    成 legacy 协议的重跑。
  - `persist_evidence` 回归修复：`rules.direction`/`rules.rules_version` 原先硬编码
    读取模块级 `STRATEGY_DIRECTIONS`/`RULES_VERSION`（v2 策略 ID 不在该表中会静默取
    到 `None`），改为读 `item.get("direction")` 与
    `protocol.get("rules_version", RULES_VERSION)`（两条路径均已回归测试覆盖）。
- `scripts/chanlun_gate_evaluation.py`：`run_evaluation`/CLI 新增 `--lineage
  {legacy,v2}`（默认 `legacy`，行为不变）；新增 `OUTPUT_FILE_V2`/`ARTIFACT_DIR_V2`
  独立产物路径（`gate_evaluation_v2_latest.json` / `evidence/gate_evaluation_v2/`），
  不与 legacy 的 `gate_evaluation_latest.json` / `evidence/gate_evaluation/` 混写或
  覆盖。
- `config/maintainability_waivers.json`：为本次改动触碰到的 4 条 `chan_signal_
  backtest.py`/`chanlun_gate_evaluation.py` 既有债务（2 条 `sys_path_mutation`、
  2 条 `long_function`，均为改动前已存在、与本次改动无关）补 waiver，过期日
  2026-10-31，与既有 waiver 条目同惯例。
- 测试：
  - `tests/test_chan_signal_backtest.py` 新增 3 条：`strategy_id_v2` 路由 +
    `is_sure` 过滤生效、12 ID 与 legacy 4 ID 各自独立且 `rules_fingerprint` 不同、
    `persist_evidence` 的 direction/rules_version 回归修复。
  - `tests/test_chanlun_gate_evaluation.py` 新增 2 条：v2 谱系合成管线自检（标注
    `pending_real_data_run`，不可作 A/B 结论）、legacy/v2 输出产物路径互不覆盖。
  - 全部新测试为**合成数据管线自检**，不构成本报告的 A/B 结论依据——结论只来自
    §2/§3 记录的 `--mode real` 真实运行。

## 7. 部署机复跑命令

```bash
PY=.venv/bin/python  # 或部署机 ~/.hermes/hermes-agent/venv/bin/python3

# 真实数据完整评估 v2 谱系（本机实测约 4 分钟：mootdx 拉取 <10s + 5000 次置换检验
# × 12 假设 × 2 变体(t1/t3) × 2 期(IS/OOS)）
$PY scripts/chanlun_gate_evaluation.py \
  --mode real --lineage v2 \
  --split 2025-08-01 \
  --start 2019-11-01 \
  --permutations 5000 \
  --json > /tmp/chanlun_gate_eval_v2.json

# 只看每个 strategy_id_v2 的判定摘要（非 JSON 模式）
$PY scripts/chanlun_gate_evaluation.py --mode real --lineage v2 \
  --split 2025-08-01 --start 2019-11-01 --permutations 5000

# legacy 四类型协议（--lineage 默认 legacy，行为与 2026-07-03 完全一致，未受本次改动影响）
$PY scripts/chanlun_gate_evaluation.py --mode real --split 2025-07-01 --start 2023-01-01

# 管线自检（无网络，合成数据，verdict 恒为 pending_real_data_run）
$PY scripts/chanlun_gate_evaluation.py --mode synthetic --lineage v2 --json

# 测试 + lint + 可维护性预算
$PY -m pytest -q tests/test_chan_signal_backtest.py tests/test_chanlun_gate_evaluation.py
$PY -m ruff check scripts/chanlun_gate_evaluation.py \
  skills/chanlun-backtest/scripts/chan_signal_backtest.py
$PY scripts/check_maintainability_budget.py
```

若 mootdx 出现"能连但 K 线接口返回 0 行"（`client.stocks()` 正常、`client.bars()`
空）：大概率是本机 `~/.mootdx/config.json` 的 `BESTIP.HQ` 缓存了一个已失效的历史最
佳节点，换一个 `tdxpy.constants.hq_hosts` 里的候选节点重连即可（不是仓库代码/网络
问题，无需改 `mootdx_source.py`）。

输出文件（不进仓库）：
- 汇总结果：`$A_STOCK_STATE_HOME/skills/chanlun-backtest/data/gate_evaluation_v2_latest.json`
- 证据产物（逐 strategy_id_v2，含 sha256 防篡改）：
  `$A_STOCK_STATE_HOME/skills/chanlun-backtest/data/evidence/gate_evaluation_v2/{strategy_id_v2}.json`
- legacy 产物路径不变：`gate_evaluation_latest.json` / `evidence/gate_evaluation/`

若要把某个 v2 信号正式注册进 `strategy_registry`（本任务未做，仅在未来某次评估给出
verdict A 时才应执行），用 `chan_signal_backtest.py --register --artifact-dir <dir>`
——该脚本在写入前会重新校验证据产物完整性；注意 `register_oos_results` 目前仍绑定
legacy 的 `chanlun_four_signal_oos` 台账键与四 ID 集合，若未来 v2 需要注册，需先扩展
该函数支持独立的 v2 台账键（本任务未做此扩展，因为不涉及注册）。
