# 第二策略（RS 领先回调）门控评估报告（2026-07-03）

对应 `docs/upgrade-plan-v2-2026-07.md` §7c「第二套策略：主题主升回调」的首次
门控评估。**结论先行：门控拒绝——统计信号显著但 OOS 样本量不足（10 < 30），
策略保持 research-only，未注册 strategy_registry；需在部署机上以全市场宇宙
复跑后再做注册决定。**

---

## 1. 策略定义与代理说明

方案原型是"主题 mainline 阶段 → 主题内龙头回调至支撑 → 介入持有 3-10 天"。
主题生命周期体系（P2）刚上线、无历史数据可回测，本次评估采用其**个股级可
回测代理** `rs_leader_pullback`（`skills/common/pullback_strategy.py`）：

| 条件 | 规则（参数固定，调参 = 重新过门控） |
|---|---|
| 趋势 | close > MA20 且 MA20 较 5 日前上行 |
| 领先（RS 代理） | 20 日收益 ≥ +15% |
| 回调 | 距 20 日高点回撤 3%~15%，且当日 low ≤ MA10 × 1.01 |
| 企稳 | 收阳且收盘不低于前收 |

入场规则与 chanlun 评估同口径：信号在历史前缀上首次可观测后**下一根 K 线
开盘价**入场（无前视）；收益按框架 T+1/T+3 净收益（含成本）口径评估。主题
过滤器（mainline 成分限定）留待门控通过后作为叠加条件接入。

## 2. 数据范围（真实运行）

- 数据源：mootdx 通达信直连（与 chanlun 评估同一路径），无新增依赖
- 标的：与 chanlun 评估相同的固定 20 只跨行业主板宇宙
- K 线：20 只共 47,556 根日线（2015-12-22 ~ 2026-07-03），基准沪深300
- 切分：split_date = 2025-07-01，置换检验 5,000 次
- 信号事件：全样本 262 个（无前视抽取），其中 OOS 10 个

## 3. 门控结果

| 指标 | 值 |
|---|---|
| OOS permutation_p | **0.0002** |
| FDR 校正后 p | **0.0010** |
| OOS alpha（每事件净超额） | **+3.30%** |
| OOS 样本量 | **10（< 30 最低要求）** |
| research_gate 判定 | **blocked（样本量不足）** |
| allowed_in_live_agent | false |

## 4. 解读（诚实边界）

1. **信号质量的初步证据是强的**：置换检验下 IS+OOS 显著性极高，OOS 每事件
   净 alpha +3.3%，方向与"强势延续 + 回调低吸"假设一致。
2. **但 10 个 OOS 样本不足以下结论**：一年 OOS 窗口里 20 只票只触发 10 次，
   小样本下的高 alpha 完全可能是运气；门控按样本量硬性拒绝是正确行为，
   本次评估**不构成**注册依据。
3. **为什么不在本机扩宇宙**：手工挑选"知名股"扩充宇宙会给动量类策略注入
   幸存者偏差（它们知名恰因过去涨得好），届时的高 alpha 不可信。无偏宇宙
   （point-in-time 全市场清单）只在部署机的 `exchange_universe.json` 里。
4. 现有 20 股宇宙同样偏向存活的大市值名单，本报告的 p 值应视为**上界乐观**
   估计——这也是复跑必须用全市场宇宙的原因。

## 5. 决定与下一步

- **决定**：`rs_leader_pullback` 保持 research-only。未写入 strategy_registry，
  不影响任何实盘排序。
- **部署机复跑**（样本量达标后门控自动给出可依赖判定）：

```bash
# 用全市场宇宙（从 exchange_universe.json 取主板代码，建议 ≥200 只）
python scripts/pullback_gate_evaluation.py --mode real --json \
  --codes $(python -c "import json;u=json.load(open('$A_STOCK_STATE_HOME/skills/stock-triage/data/exchange_universe.json'));print(' '.join(sorted(u.get('codes') or [])[:400]))")
```

- 若复跑 `oos_sample_count ≥ 30` 且门控 `passed_for_reference` → 走
  strategy_registry 注册流程 + 接入主题 mainline 过滤器；
- 若复跑不显著 → 归档，结论与 chanlun 四信号一致处理。

## 6. 代码与可复现性

- 信号：`skills/common/pullback_strategy.py`（纯前缀函数，参数常量化）
- 评估：`scripts/pullback_gate_evaluation.py`（`--mode synthetic` 仅管线自检，
  结构上不可能输出 A/B 结论）
- 复用机件：`chan_signal_backtest` 的事件抽取（自定义 analyzer 注入）、
  IS/OOS 切分、置换 + FDR、对照组、`research_gate` 判定、证据落盘
- rules/dataset 双指纹随结果落盘，复跑可验证一致性
