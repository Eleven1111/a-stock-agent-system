---
name: chanlun-backtest
description: >-
  缠论与打板规则的离线研究闸门。用于检查规则是否完成样本内锁定、样本外一次性验证、
  成本模型、对照组、统计检验和全变体报告。只做研究验证，不输出实时买卖指令。
version: 1.1.0
author: Luna
metadata:
  hermes:
    tags: [A股, 缠论, 回测, 统计检验, 策略研究, 分形图]
    category: finance
---

# 缠论/打板离线研究闸门

本 skill 将 `Eleven1111/chanlun-backtest@f25b36a` 融入当前 A 股 Agent 系统，定位为策略研究与上线前验证层。它不替代 `stock-triage` 的实时编排，也不生成实时买入建议。

核心原则：
- 只检验机械规则，不检验主观复盘语言。
- 样本内开发和样本外验证必须隔离。
- OOS 结果只能在规则锁定后运行一次。
- 必须扣交易成本。
- 必须报告所有变体和对照组，不能只挑最好结果。

## 适用场景

- 用户问"缠论有没有 edge""三买能不能跑赢指数"。
- 要把某个缠论形态、打板过滤器或出场规则接入日常 Agent 前，先判断是否完成研究验证。
- 要检查一份回测报告是否足以支持策略上线。
- **用户要求画分形图/走势图时** — 必须用 `scripts/fractal_chart.py` 输出 Markdown 分形图，禁止用普通 ASCII K线图替代。

## 五阶段流程

1. 数据获取：A 股/港股 OHLCV，明确 IS/OOS 切分。
2. 信号生成：去包含、分型、笔、线段、中枢、三买/三卖或打板规则。
3. 回测执行：包含成本、滑点、仓位、持仓约束。
4. 统计检验：t 检验、bootstrap、permutation test、FDR 校正。
5. 报告输出：所有变体、对照组、年度稳定性、结论句。

## 研究闸门 CLI

```bash
PY=~/.hermes/hermes-agent/venv/bin/python3
SDIR=~/.hermes/skills/chanlun-backtest/scripts

# 示例研究状态
$PY $SDIR/research_gate.py --example --json

# 检查一份研究状态文件
$PY $SDIR/research_gate.py --input research_state.json --json
```

输入 JSON 示例：

```json
{
  "strategy_id": "chanlun_third_buy_loose",
  "phase": "pre_oos",
  "rules_locked": true,
  "has_costs": true,
  "reports_all_variants": true,
  "controls": ["random_entry", "simple_breakout", "buy_hold"],
  "oos_run_count": 0,
  "changed_after_oos": false
}
```

输出会给出：

- `decision`：`blocked`、`ready_for_oos`、`passed_for_reference`、`failed`。
- `blocking_reasons`：不能进入下一阶段的原因。
- `allowed_in_live_agent`：是否允许作为已验证研究结论供日常 Agent 引用。
- `next_actions`：下一步该补什么证据。

## 缠论分形图 CLI

`scripts/fractal_chart.py` 是独立的分形图绘制工具（无第三方依赖，仅 urllib+json）：

```bash
PY=~/.hermes/hermes-agent/venv/bin/python3
SDIR=~/.hermes/skills/chanlun-backtest/scripts

# 默认60日
$PY $SDIR/fractal_chart.py 300255 常山药业

# 指定天数
$PY $SDIR/fractal_chart.py 603859 能科科技 --days 30 --height 12

# 纯代码查询
$PY $SDIR/fractal_chart.py 600519
```

**数据源：** 腾讯 ifzq.gtimg.cn 前复权日线（免费、全天候、无需代理）
**输出结构：** Markdown格式，包含标题→MA排列→K线图→顶分型↑/底分型↓→分型明细表→统计

**腾讯API注意事项：**
- param 必须带 sh/sz 前缀，如 `param=sh603859,day,,,60,qfq`
- 返回格式：`[date, open, close, high, low, volume_or_amount]`
- 今日K线的volume字段可能是dict（含除权信息），脚本已处理

## 缠论结构信号 CLI（chan_structure.py）

`scripts/chan_structure.py` 把缠论从"画图"升级为"出信号"：在分型基础上做
去包含 → 笔 → 中枢 → 三买/三卖 → MACD 背驰，输出**结构化 JSON 信号**供 `four_dim_scorer` 消费。

```bash
PY=~/.hermes/hermes-agent/venv/bin/python3
SDIR=~/.hermes/skills/chanlun-backtest/scripts

$PY $SDIR/chan_structure.py 600519 贵州茅台 --json
$PY $SDIR/chan_structure.py 000001 --days 120
```

输出含 `structure`（笔/中枢统计、最新中枢 zd/zg）与 `signals`
（`third_buy`/`third_sell`/`top_divergence`/`bottom_divergence`，各带 `strategy_id`）。

## 四信号正式 IS/OOS 回测

`scripts/chan_signal_backtest.py` 分别验证四个可执行信号 ID。它按历史 K 线逐日前推，
只在信号首次可观察后的下一交易日开盘入场，禁止用结构信号所指向的历史 K 线价格入场。
买入后最早在再下一个交易日收盘计 T+1 收益，不能用买入当天收盘模拟卖出。
三卖/顶背驰使用方向归一化收益，价格下跌才记为正收益，但其角色是验证
“应回避/阻断买入”的预测能力，不宣称 A 股个股可直接做空。多空两类收益都扣同方向的
成本拖累，禁止把成本反号变成熊信号的虚假正收益；多头样本还会排除下一日不可成交的一字板。

```bash
$PY $SDIR/chan_signal_backtest.py \
  --input chan_research_dataset.json \
  --split 2025-01-01 \
  --min-oos-samples 30 \
  --artifact-dir chan-oos-artifacts \
  --register \
  --json
```

输入必须包含 `series`（多股票前复权日线）和 `benchmark_bars`（同期间基准日线）。
输出为四个独立 `research_state` 和闸门结论，不再用打板竞价 MVP 的结论代替缠论信号验证。
样本不足会被研究闸门直接阻断。`--register` 会把规则、切分日和数据集指纹写入
`chanlun_oos_runs.json`；相同输入可幂等重跑，但更换规则、切分或数据后不能覆盖既有 OOS，
必须使用版本化策略 ID 和新的留出集重新立项。

## 完整组合级回放

单因子均值不能代表系统选股能力。`portfolio_backtest.py` 对决策时已经落盘的候选快照
执行逐日组合回放，统一处理 Top N、现金/仓位、100 股整数手、成本滑点、一字板、停牌、
A 股 T+1、基准和逐维消融：

```bash
$PY $SDIR/portfolio_backtest.py \
  --input portfolio_backtest_input.json \
  --split 2025-01-01 \
  --artifact portfolio_backtest_oos.json \
  --json
```

禁止用今天的数据重建过去候选。完整输入契约、证据边界和旧打板缓存迁移方式见
`docs/portfolio-research-protocol.md`。

**信号过闸才计权（铁律）：** chan_structure 只产出"研究假设"信号。四维技术面对这些信号，
在对应 `strategy_id` 通过 `research_gate --register`（写入 `strategy_registry`，
`allowed_in_live_agent=true`）之前，一律 display-only / 0 权重。把闸门结论登记进注册表：

```bash
$PY $SDIR/research_gate.py --input research_state.json --register --json
```

## 接入规则

`stock-triage` 可以引用本 skill 的研究结论，但不能把它当作实时选股器。实时打板候选由 `daban-stock-picker` 负责，且必须继续经过可成交性和持仓闸门。

在主线架构中，本 skill 位于 **Research Validation / Strategy Admission** 层：

1. `research_gate --register` 决定某类缠论结构信号是否允许进入实盘 Policy。
2. `candidate-discovery` 只对已固化的 K 线输入快照运行 `chan_structure.analyze`。
3. 近期结构证据随候选池进入 09:26 竞价和 09:35 开盘确认。
4. 未过闸信号只展示；已过闸的三卖/顶背驰可以阻断买入。
5. 已过闸的三买/底背驰只能增强证据，不得绕过公告、可成交性、组合风险和市场状态。
6. 实盘结算保留主策略，同时把缠论信号写入 `strategy_attributions`。绩效层按多空方向
   归一化输出共现期望；该统计用于监控证据有效性，不宣称是主策略收益的因果拆分。

如果研究闸门返回 `blocked` 或 `failed`，对应参数只能标注为"研究假设"，不能标注为"已验证有效策略"。

缠论结构信号同理：未经 `research_gate --register` 登记为 `allowed_in_live_agent` 的信号，
在 `four_dim_scorer` 中只展示、不计权（由 `common/strategy_registry.py` 统一裁决）。
打板阈值由 `config/daban_thresholds.yaml` 单一事实源管理，回测引擎与实盘候选共读，
阈值变更只允许在 `research_gate` 通过后进行。
