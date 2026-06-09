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
$PY $SDIR/fractal_chart.py 600011
```

**数据源：** 腾讯 ifzq.gtimg.cn 前复权日线（免费、全天候、无需代理）
**输出结构：** Markdown格式，包含标题→MA排列→K线图→顶分型↑/底分型↓→分型明细表→统计

**腾讯API注意事项：**
- param 必须带 sh/sz 前缀，如 `param=sh603859,day,,,60,qfq`
- 返回格式：`[date, open, close, high, low, volume_or_amount]`
- 今日K线的volume字段可能是dict（含除权信息），脚本已处理

## 接入规则

`stock-triage` 可以引用本 skill 的研究结论，但不能把它当作实时选股器。实时打板候选由 `daban-stock-picker` 负责，且必须继续经过可成交性和持仓闸门。

如果研究闸门返回 `blocked` 或 `failed`，对应参数只能标注为"研究假设"，不能标注为"已验证有效策略"。
