# chanlun 一次性门控评估报告（2026-07-03）

对应 `docs/upgrade-plan-v2-2026-07.md` §5「chanlun 定位落定」的一次性评估任务（5a）。
本报告是二选一定位决策的依据：**结果 A**（通过门控 → 可注册进
`strategy_registry`，本任务不做注册动作）或 **结果 B**（不通过 → 降格为结构位置
过滤器，只做证据不做信号）。

**结论先行：结果 B——chanlun 四类结构信号在当前样本上均未通过 OOS 门控，建议降格为结构位置过滤器。**

---

## 1. 评估对象

框架已实现且已接入 `research_gate` 的四个可执行结构信号（`skills/chanlun-backtest/scripts/chan_structure.py` +
`chan_signal_backtest.py`），本次评估**没有新发明规则**，只是把框架第一次真正跑通到底：

| strategy_id | 缠论形态 | 方向 | 入场规则 |
|---|---|---|---|
| `chanlun_third_buy` | 三买（离开中枢后回踩不破中枢上沿 ZG） | 看多 | 信号确认次日开盘价入场 |
| `chanlun_third_sell` | 三卖（离开中枢后反弹不过中枢下沿 ZD） | 看空 | 同上（作规避信号统计，非做空） |
| `chanlun_bottom_divergence` | 底背驰（价创新低但 MACD 柱面积走弱） | 看多 | 同上 |
| `chanlun_top_divergence` | 顶背驰（价创新高但 MACD 柱面积走弱） | 看空 | 同上 |

入场规则（`entry_rule = first_detection_then_next_bar_open`）：信号必须在历史前缀
上首次被 `chan_structure.analyze()` 检测到之后，才允许在**下一根K线开盘价**入场——
杜绝用信号自身回溯出的结构索引做事后入场（无前视偏差）。

## 2. 数据范围（真实运行）

- **数据源**：mootdx（通达信 TCP 直连），本机 ClashX TUN 模式下实测可用，无第三方新增依赖（mootdx 已在 `.venv` 中）。
- **标的**（20 只，固定 universe，跨行业选取，非指数成分自动拉取，避免额外的指数成分缓存依赖）：
  600519 贵州茅台、000001 平安银行、600036 招商银行、000651 格力电器、601318 中国平安、
  300750 宁德时代、002415 海康威视、600030 中信证券、000333 美的集团、601899 紫金矿业、
  600000 浦发银行、000002 万科A、601166 兴业银行、600276 恒瑞医药、000858 五粮液、
  601888 中国中免、600809 山西汾酒、000725 京东方A、601012 隆基绿能、300059 东方财富。
  全部 20 只均成功拉取，无因历史不足被跳过（`skipped_short_history: []`）。
- **基准**：沪深300指数（000300），经 mootdx `index_bars()` 获取（个股 `bars()` 的
  symbol 空间不含指数代码，探针已确认返回 0 行；本任务在 `mootdx_source.py` 新增
  `fetch_index_daily()`，走 `index_bars()`，见 §6 代码改动）。
- **时间范围**：2019-11-18 ~ 2026-07-03（20 只个股共 32,000 根日K；基准 1,600 根日K，2019-11-26 起）。
- **IS/OOS 切分日**：2025-07-01（样本内 ~5.6 年，样本外 ~1 年）。
- **事件样本**：全部 series 共检测到 1,007 个原始事件（去重后按 4 个 strategy_id 分桶，见下表）。
- **对照组**：`random_entry`（同池 20% 稳定哈希采样）、`simple_breakout`（20 日收盘突破/破位）、`buy_hold`（沪深300 逐日开-收基准，方向标准化）。三组均有真实样本（无 `missing` 阻断项）。
- **统计检验**：单样本 t 检验（正态近似）、bootstrap 置信区间（2000 次重采样）、
  置换检验（5000 次，signal vs 最强对照组）、Benjamini-Hochberg FDR 校正（q=0.10，4 个假设一起校正）。
- **数据集/规则指纹**（写入证据产物，供复跑校验）：
  - `rules_fingerprint`: `214eca133d98079acf77bcf8c46e72d107068e87faca3929a7c60d523cb8a0b1`
  - `dataset_fingerprint`: `c04a0a2d0e666313b134c785ac61ef9616dd2fc14326f507c6a1a4457edbe996`

## 3. IS/OOS 结果与统计检验

主检验字段为 T+1 净收益（`t1_return`，已扣佣金/滑点/印花税，方向标准化：看空信号收益 = -gross）。

| strategy_id | 样本(IS/OOS) | OOS permutation_p | FDR-adjusted p (q=0.10) | OOS alpha (signal-control) | 最强对照 | gate 判定 | 阻断原因 |
|---|---|---|---|---|---|---|---|
| `chanlun_third_buy` | 40 / 11 | 0.0276 | 0.1104 | **-0.0092**（signal均值-0.0122 vs buy_hold均值-0.0030） | buy_hold | `blocked` | 样本量不足: 11<30 |
| `chanlun_bottom_divergence` | 419 / 89 | 0.4279 | 0.5706 | -0.0016 | buy_hold | `failed` | 无（样本充足但不显著） |
| `chanlun_third_sell` | 51 / 6 | 0.8450 | 0.8450 | +0.0018 | random_entry | `blocked` | 样本量不足: 6<30 |
| `chanlun_top_divergence` | 326 / 65 | 0.1032 | 0.2064 | -0.0051 | random_entry | `failed` | 无（样本充足但不显著） |

逐条解读：

- **`chanlun_third_buy`**：OOS 置换检验 p=0.0276 看似"显著"，但方向是反的——OOS
  期信号组均值 -1.22%、跑输最强对照（buy_hold，-0.30%），即"显著地跑输基准"，
  `oos_alpha` 为负。经 FDR 校正后 p=0.1104，且 OOS 事件数（11）低于最低样本量门槛
  （30），双重不满足，`research_gate` 正确判定 `blocked`。三买回踩确认在本样本
  上没有正向 edge。
- **`chanlun_bottom_divergence` / `chanlun_top_divergence`**：这两个背驰信号样本量
  充足（OOS 89 / 65），但 permutation_p 均不显著（0.43 / 0.10），FDR 校正后更不显著
  （0.57 / 0.21），`oos_alpha` 均为负，`research_gate` 判定 `failed`——样本充分但
  统计上无法拒绝"无 edge"的原假设。
- **`chanlun_third_sell`**：OOS 事件仅 6 个，远低于最低样本量要求，且方向上也没有
  看空信号该有的正向收益（`oos_alpha`=+0.0018 但 p=0.845，噪音水平）。三卖形态在
  本 universe/时间窗内触发太少，无法评估。

**四个假设一起做 FDR 校正**（Benjamini-Hochberg，q=0.10）后，没有一个信号的
adjusted p 落在拒绝域内。

## 4. 门控判定汇总

| strategy_id | `research_gate.evaluate_gate` decision | `allowed_in_live_agent` |
|---|---|---|
| chanlun_third_buy | blocked | false |
| chanlun_bottom_divergence | failed | false |
| chanlun_third_sell | blocked | false |
| chanlun_top_divergence | failed | false |

四个信号全部未通过门控（`blocked` 或 `failed`，均不等于 `passed_for_reference`）。
`allowed_in_live_agent` 全部为 `false`。本次评估**没有**调用 `--register`，
`strategy_registry` 未被写入，符合任务硬约束（未通过 OOS 门控的策略不得影响 live 排序）。

## 5. 定位建议

**verdict: B（结构位置过滤器）**

四个已实现的缠论结构信号（三买/三卖/顶背驰/底背驰）在 20 只跨行业主板股票、
2019-2026 年真实历史数据、1 年 OOS 窗口下，均未能通过 IS/OOS + 置换检验 + FDR
门控。其中样本充足的两个背驰信号（bottom/top divergence，OOS 均 >60 个事件）
统计上明确不显著；样本不足的两个三买/三卖信号无法下结论，但即使把它们的点估计
当真，`oos_alpha` 符号也谈不上稳定为正。

**按 upgrade-plan-v2 §5 的既定路线执行结果 B**：

1. chanlun 降格为"结构位置过滤器"，不作为独立信号源参与打分/排序；
2. 在证据包新增 `structure_position` section（当前价格处于笔/线段的什么位置、
   是否背驰）供 `risk_redteam` 引用（如"线段末端背驰" → risk_flag，降低追高倾向）；
   ——**此为集成动作，不在本任务范围，留给 §5 后续 P3 执行**；
3. 文档需写明"chanlun 仅作位置证据，不预测方向"，停掉与信号化无关的维护投入。

本报告本身不做任何注册/集成变更，只产出评估证据。

## 6. 代码改动

- 新增 `scripts/chanlun_gate_evaluation.py`：一次性评估 runner，真实模式经 mootdx
  拉取 20 只固定 universe 个股 + 沪深300 基准日线，组装 `chan_signal_backtest.analyze_payload()`
  所需 payload，跑完整 IS/OOS + 统计检验 + 证据产物落盘，输出结构化 JSON 到
  `paths.data_file("chanlun-backtest", "gate_evaluation_latest.json")`；`--mode synthetic`
  提供纯管线自检路径，输出永远标注 `verdict: pending_real_data_run`，不可用于 A/B 结论。
- `skills/chanlun-backtest/scripts/mootdx_source.py` 新增 `fetch_index_daily()`：
  mootdx 个股 `client.bars()` 的 symbol 空间不含指数代码（探针实测 000300 在
  `bars()` 下返回 0 行），需改走 `client.index_bars()`；分页/去重逻辑与既有
  `fetch_daily()` 一致，无新增依赖。
- 测试：`tests/test_chanlun_gate_evaluation.py`（synthetic 全流程、real 模式 mock
  mootdx 验证 index_bars 调用路径、短历史标的过滤、verdict A/B 判定、证据产物落盘）；
  `tests/test_mootdx_source.py` 新增 `fetch_index_daily` 用例。

## 7. 部署机复跑命令

```bash
PY=.venv/bin/python  # 或部署机 ~/.hermes/hermes-agent/venv/bin/python3

# 真实数据完整评估（约 1-2 分钟：mootdx 拉取 <5s + 5000 次置换检验 ×8 变体）
$PY scripts/chanlun_gate_evaluation.py \
  --mode real \
  --split 2025-07-01 \
  --start 2023-01-01 \
  --permutations 5000 \
  --json > /tmp/chanlun_gate_eval.json

# 只看每个 strategy_id 的判定摘要（非 JSON 模式）
$PY scripts/chanlun_gate_evaluation.py --mode real --split 2025-07-01 --start 2023-01-01

# 管线自检（无网络，合成数据，verdict 恒为 pending_real_data_run）
$PY scripts/chanlun_gate_evaluation.py --mode synthetic --json

# 换 universe（默认 20 只跨行业主板股）
$PY scripts/chanlun_gate_evaluation.py --mode real --split 2025-07-01 --start 2023-01-01 \
  --codes 600519 000001 600036 ...

# 测试 + lint
$PY -m pytest -q tests/test_chanlun_gate_evaluation.py tests/test_mootdx_source.py
$PY -m ruff check scripts/chanlun_gate_evaluation.py skills/chanlun-backtest/scripts/mootdx_source.py
```

输出文件（不进仓库）：
- 汇总结果：`$A_STOCK_STATE_HOME/skills/chanlun-backtest/data/gate_evaluation_latest.json`
  （默认 `A_STOCK_STATE_HOME` 未设时回退 `~/.hermes`）
- 证据产物（逐 strategy_id，含 sha256 防篡改）：
  `$A_STOCK_STATE_HOME/skills/chanlun-backtest/data/evidence/gate_evaluation/{strategy_id}.json`

若要把某个信号正式注册进 `strategy_registry`（本任务未做，仅在未来结果 A 成立时才应执行），
用 `skills/chanlun-backtest/scripts/chan_signal_backtest.py --register --artifact-dir <dir>`
——该脚本在写入前会重新校验证据产物完整性，并拒绝"规则/数据集指纹变了却复用同一 split"
的重复提交。
