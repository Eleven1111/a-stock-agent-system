# 打板候选池过滤规格

本规格来自 `Eleven1111/chanlun-backtest@f25b36a` 的打板规则，改造成当前 A 股 Agent 系统可直接消费的 JSON 闸门。

## 股票池

| 条件 | 规则 |
|------|------|
| 市场 | 仅 A 股主板 10cm，排除创业板、科创板、北交所 |
| ST | `is_st` 标记（ST股限制已解除） |
| 上市时间 | `listed_days >= 60` |
| 流通市值 | `15亿 <= float_market_cap <= 120亿` |
| 成交活度 | `avg_turnover_amount_20d >= 2亿` |
| 价格带 | `4元 <= close_prev <= 35元` |

## 市场与持仓闸门

| 条件 | 规则 |
|------|------|
| 强股反馈 | `yday_limitup_index_open > -2%` |
| 炸板风险 | `broken_rate_first20m <= 35%` |
| 处置优先 | `has_positions_to_dispose == false` 才允许新开仓 |
| 周频率 | `week_trades < 3` |
| 日停手 | `day_loss_pct > -2%` |
| 周冻结 | `week_loss_pct > -5%` |
| 连错冻结 | `consecutive_losses < 3` |

## 首板回封

| 条件 | 规则 |
|------|------|
| 首次上板 | `first_limitup_time <= 10:30` |
| 炸板次数 | `open_board_count <= 2` |
| 回封耗时 | `reseal_minutes <= 15` |
| 封单强度 | `seal_amount / float_market_cap >= 0.3%` |
| 主动买入 | `active_buy_ratio >= 60%` |
| 大单流入 | `big_order_net_inflow_ratio >= 8%` |
| 板块集群 | `sector_limitup_count >= 3` |

## 二板弱转强

| 条件 | 规则 |
|------|------|
| 前日状态 | `prev_day_limitup_close == true` |
| 竞价窗口 | `-1% <= auction_gap_pct <= 3%` |
| 早盘上板 | `first_limitup_time <= 09:45` |
| 题材跟随 | `sector_companion_count >= 2` |

## 六问否决

开仓前逐项判断：

1. 今天短线情绪评分是否 `>= 7`。
2. 有没有明确主线板块。
3. 板块内涨停是否 `>= 3` 只。
4. 目标股是不是龙头或前排。
5. 是否为 `09:35-10:30` 早盘强回封。
6. 次日低开是否愿意机械卖出。

任意 2 项或更多为否，候选直接否决。

## 可成交性

`skills/common/tradeability.py` 是统一闸门：

- 停牌/行情缺失：不可买。
- 一字封死涨停：不可买。
- 普通封板：标记 `risky`，允许进入候选但必须披露排队成交风险。
- 跌停：标记 `risky`，不作为打板买入候选。

## 仓位与出场

- 初始试错仓：20%。
- 单票上限：50%。
- 当日总仓上限：60%。
- T+1 早盘先处理已有持仓，再考虑新开仓。
- T+1 低开低于买入价 3% 且主线走弱，执行机械卖出。
- T+1 高开 6%-9%，09:30-09:45 卖出 1/2，余仓观察是否继续封板或跌破 5 日线。
