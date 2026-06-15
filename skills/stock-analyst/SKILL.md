---
name: stock-analyst
description: >-
  A-share technical and fundamental analysis skill. It supports single-stock
  analysis, full-market screening, sector comparison, charting, and sourced
  news checks without treating runtime watchlists as static configuration.
version: 3.2.0
author: Luna
metadata:
  hermes:
    tags: [A股, 技术分析, 选股, 全市场扫描, 基本面, 新闻]
    category: finance
---

# 股票分析工具

本技能负责技术面、基础财务、筛选和可视化。它可以提供候选证据，但最终方向性建议仍需经过
`stock-triage` 的公告、可成交性、组合风险和 T+1 Policy。

## 数据边界

- 行情与 K 线通过统一 provider adapter 获取，响应必须校验并记录来源时间。
- 新闻和搜索热度必须附可验证来源；API 失败时不得模拟结果。
- “全市场”必须枚举完整合格股票池，不得用预设列表替代。
- 实时关注对象从 `runtime_targets.py` 读取，不在本文件保存。
- 板块参数是用户当前命令的筛选条件，不是买入理由，也不降低评分门槛。

## 工作流

单股分析：

1. 拉取当前报价和前复权 K 线。
2. 计算 MA、MACD、RSI、KDJ、布林带和量能。
3. 检查财务趋势与估值解释，避免把低 PE 单独当作利好。
4. 拉取公告和可验证新闻。
5. 输出技术证据、失效条件和数据质量，不越过 Triage 直接承诺可执行交易。

全市场筛选：

1. 通过证券列表 adapter 构建完整股票池。
2. 批量行情初筛。
3. 对有限候选补充 K 线、财务和公告证据。
4. 记录每层数量、淘汰原因、数据覆盖率和快照版本。
5. 交给 Triage 做策略与组合风险裁决。

## 客观约束

- 空头排列时限制正向技术评分。
- 价格低于 MA20 时，超卖只能说明位置，不能单独证明企稳。
- 评分不足时不得输出买入评级。
- 低估值必须与利润、现金流和行业周期交叉验证。
- 追高分析必须明确当前阶段、乖离、换手、题材持续性和失败场景。
- 数据不足时输出“观察”或“无法判断”，不补写缺失事实。
- 涉及持仓时以 `portfolio_manager.py --balance --json` 为唯一账户事实源。
- 卖出、减仓或止损建议必须先计算 A 股 T+1 可执行日期。

## 命令

```bash
PY=python
ANALYST=skills/stock-analyst/analyst.py

$PY $ANALYST analyze <code> <name>
$PY $ANALYST weekly <code> <name>
$PY $ANALYST realtime <code1>,<code2>
$PY $ANALYST chart <code> <name> 30
$PY $ANALYST fundamental <code> <name>
$PY $ANALYST screener "<condition>"
$PY $ANALYST sector-scan
$PY $ANALYST news <code> <name>
$PY $ANALYST news sector <sector>
$PY $ANALYST news market
$PY $ANALYST news trend <keyword>
```

`screen` 和 `compare` 只接受调用方明确给出的板块，不存在隐式默认主题。需要真正的全量结果时
使用 `sector_scan.py` 或 `screener`，并披露扫描覆盖率。

## 输出

结果至少包含：

- 报价与 K 线的 `asof`、复权方式和来源。
- 趋势、动量、波动和量能证据。
- 财务趋势、估值解释和公告风险。
- 数据缺口、降级路径和置信度。
- 支撑、阻力、失效条件和观察条件。
- 若进入方向性建议链路，交由 Triage 补全价格计划、仓位、持有周期和 T+1。

## 关联能力

- `stock-triage`：最终 Policy 与账本。
- `hot-money-tactics`：情绪周期和涨停梯队。
- `news-to-sector`：催化传导。
- `serenity-investment-research`：公司深研。
- `chanlun-backtest`：结构证据和研究闸门。

## 验证

```bash
pytest -q tests/test_stock_analyst*.py tests/test_data_provider.py
python -m ruff check skills/stock-analyst
```
