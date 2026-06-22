# 游资主线龙头选股协议

本协议把“择时 -> 主线板块 -> 板块龙头 -> 竞价 -> 开盘承接 -> 盘中确认”接入现有候选发现、策略门禁、统一账本和 T+1/T+3 反馈链路。它是研究假设，不是绕过研究闸门的实盘规则。

## 可靠性边界

- D0 游资状态只消费 `candidate-discovery-input` 已固化的全市场行情、梯队和社会关注快照，不增加网络请求。
- 输入快照读回后再计算，派生状态写入内容寻址快照和 `hot_money_selection_latest.json`。
- 梯队日期缺失、来自未来、不是事件当日，或全市场行情覆盖不足时，打板 lane 关闭。
- 板块映射覆盖不足、没有同时满足主线排名和涨停集群的板块时，打板 lane 关闭。
- 上述失败不关闭趋势 lane，避免单一游资数据源故障导致整个候选系统停摆。
- 09:50 和 13:15 各只复用 09:35 前五候选，最多发起一次 20 只腾讯行情批量请求。

## D0 择时

`hot_money_selection.build_market_timing` 从同一全市场快照计算：

- 上涨、下跌、平盘家数
- 按 A 股板块涨跌停规则识别的涨停/跌停家数
- 昨日梯队股票的当日平均溢价
- 现有高度板、晋级率、退潮规则和 `allow_new_daban`

`daban_ready=true` 必须同时满足：行情数量达到配置门槛、梯队时钟有效、市场温度允许新开打板研究仓。失败原因进入 `reasons`，不再回退为无约束 neutral。

## D0 主线板块

板块分数是同日横截面研究分，不是收益承诺：

| 因子 | 默认研究权重 |
|---|---:|
| 板块涨停家数 | 45% |
| 板块成交额 | 20% |
| 板块前十涨幅均值 | 25% |
| 多源社会关注度 | 10% |

只有排名前二、涨停不少于三只、板块映射覆盖达标的板块可进入打板研究 lane。与前一交易日排名对比后标记：

- `confirmed`：连续位于主线前二
- `emerging`：新进入主线前二
- `weakening`：前一日主线，本日跌出
- `neutral`：其他板块

这些阈值集中在 `config/candidate_selection.json`，变更后必须重新做时间切分 OOS 验证。

## D0 龙头身份

候选在所属板块内按连板高度、既有游资证据、涨幅和成交额排序。默认只允许板块前二进入打板研究 lane：

- `sector_leader`：板块第一
- `sector_core`：板块第二
- `sector_follower`：其余

通过门禁的候选使用独立策略标识 `daban:mainline_leader_confirm`。系统不再把普通量价高分候选误标为 `daban:first_board_reseal`。

该新策略默认未注册，因此 `strategy_registry` 会把正向建议降为 `watch`、仓位归零。只有独立研究产物通过现有 OOS 闸门并注册后，才可能进入 live policy。

## D1 确认链路

| 时间 | 作用 | 输出 |
|---|---|---|
| 09:25 | 竞价收口 | 竞价分、板块内竞价排名、一字板/缺失拒绝 |
| 09:35 | 开盘确认 | 公告质检、可成交性、板块内开盘排名、策略门禁 |
| 09:50 | 承接复核 | `confirmed/watch/invalidated` 研究状态 |
| 13:15 | 午后回流 | `confirmed/watch/invalidated` 研究状态 |

09:50/13:15 不产生订单，不新增推荐记录，也不建议当日卖出。每条记录固定包含 `execution_action=none`、`same_day_sell_allowed=false` 和下一交易日最早卖出日期。

## 证据和反馈

`selection_context` 随候选贯穿：

- D0 候选池和候选生命周期
- 09:25 竞价决策
- 09:35 推荐审计与 `signal.opened`
- 组合研究快照
- 09:50/13:15 候选生命周期事件

字段包含市场时点、板块名称/排名/状态、板块内龙头排名、确认窗口和不可变快照引用。T+1/T+3 结算后可以按这些维度做归因，而不是只评估一个混合总分。

## 运行与验证

```bash
python skills/stock-triage/scripts/candidate_discovery.py --json
python skills/daban-stock-picker/scripts/auction_collector.py --finalize --json
python skills/daban-stock-picker/scripts/open_confirmation.py --json
python skills/daban-stock-picker/scripts/hot_money_checkpoint.py --profile morning_confirm --json
python skills/daban-stock-picker/scripts/hot_money_checkpoint.py --profile afternoon_reflow --json
python scripts/validate_cron_manifest.py cron/hermes-cron-manifest.json
```

上线研究验证至少按交易日切分训练/验证/测试区间，并分别检查：主线前二相对全市场、板块龙一/龙二相对板块、竞价确认增量、09:50 和 13:15 确认增量。未通过的子信号保持研究态，不能只因组合分提高就注册实盘。
