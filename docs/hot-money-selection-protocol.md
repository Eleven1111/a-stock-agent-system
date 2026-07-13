# 游资主线龙头选股协议

本协议把“择时 -> 主线板块 -> 板块龙头 -> 竞价 -> 开盘承接 -> 盘中确认”接入现有候选发现、策略门禁、统一账本和 T+1/T+3 反馈链路。它是研究假设，不是绕过研究闸门的实盘规则。

## 可靠性边界

- D0 游资状态只消费 `candidate-discovery-input` 已固化的全市场行情、梯队和社会关注快照，不增加网络请求。
- 输入快照读回后再计算，派生状态写入内容寻址快照和 `hot_money_selection_latest.json`。
- 梯队日期缺失、来自未来、不是事件当日，或全市场行情覆盖不足时，打板 lane 关闭。
- 板块映射覆盖不足、没有同时满足主线排名和涨停集群的板块时，打板 lane 关闭。
- 上述失败不关闭趋势 lane，避免单一游资数据源故障导致整个候选系统停摆。
- 09:50 和 13:15 各只复用 09:35 前五候选，最多发起一次 20 只腾讯行情批量请求。
- K 线深度池默认增强 1000 只、保留 500 只观察标的；这不是全市场覆盖声明。
- 09:15-09:23 对深度池采集分钟级五档，09:24 对全部合格股票补一张轻量竞价快照。池外异动只进入研究情报，不能进入执行短名单。

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

## 拥挤、脆弱与市场状态

同一 D0 快照还输出市场/板块拥挤度、脆弱度和 S0-S6 状态分布。它们是纯日线启发式
分数，不是经过训练校准的概率；输出明确包含 `calibrated=false`。S6 只会在退潮硬
证据占优时成为主导状态，统一 Policy 会把正向建议的仓位倍率压到最多 20%。高拥挤
与高脆弱的横截面组合仍默认只观察，只有显式启用护栏才进一步减仓。

09:25 竞价与 09:35 开盘确认必须消费同一个 `selection_context.market_timing`，不能在
竞价阶段绕过退潮状态。

## 玩家反身性状态

`skills/common/reflexivity.py` 在同一不可变快照上生成
`reflexivity_state_v1`。它严格区分可观测事实与玩家推断：首封时间、板块阶段、
龙头消融和市场状态属于事实；`algorithmic_pattern` 只是疑似算法化交易形态概率，
不得表述为已经识别出量化机构。

所有反身性阈值的单一事实源是 `config/reflexivity_strategy.json`。每条状态都携带
`strategy_version` 与规范化 `config_sha256`；更改任一阈值或版本都会产生新指纹。
消融报告拒绝混合不同指纹，可用 `--config-sha256 <digest>` 固定只评估预提交版本。

当前反馈阶段为 `ignition / diffusion / saturation / distribution / collapse`；证据
不足时固定输出 `unknown`，不回填中性状态。第一批策略只允许防守性作用：

| 策略标识 | 触发证据 | live 作用 |
|---|---|---|
| `leader_isolation_exit_v1` | 龙头消融后无板块宽度，且板块已 weakening | 禁止新增打板仓 |
| `algorithmic_false_consensus_guard_v1` | 09:25–09:31 抢封、板块证据不足、高拥挤且高脆弱 | 打板仓位上限减半 |
| `institution_distribution_guard_v1` | 机构龙虎榜净卖出与市场拥挤同时出现 | 禁止追入 |

反身性处于 `ignition` 或 `diffusion` 不会形成正向准入，也不能绕过
`strategy_registry`。正向策略只有在独立 point-in-time OOS、shadow、人工晋级后才可
获得非零权重。

## D1 确认链路

| 时间 | 作用 | 输出 |
|---|---|---|
| 09:25 | 竞价收口 | 竞价分、板块内竞价排名、一字板/缺失拒绝 |
| 09:35 | 开盘确认 | 公告质检、可成交性、板块内开盘排名、策略门禁 |
| 09:50 | 承接复核 | `confirmed/watch/invalidated` 研究状态 |
| 13:15 | 午后回流 | `confirmed/watch/invalidated` 研究状态 |

09:50/13:15 不产生订单，不新增推荐记录，也不建议当日卖出。每条记录固定包含 `execution_action=none`、`same_day_sell_allowed=false` 和下一交易日最早卖出日期。

08:50、09:27、09:36 的独立简报分别展示打板/趋势 TOP、全市场竞价涨跌与被过滤高分票。简报只读已落盘结果；它不复算评分、不写信号账本，也不改变策略准入。

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
python scripts/reflexivity_report.py --outcome t3_close_ret --round-trip-cost-bps 20 \
  --config-sha256 <frozen-config-digest>
python scripts/validate_cron_manifest.py cron/hermes-cron-manifest.json
```

上线研究验证至少按交易日切分训练/验证/测试区间，并分别检查：主线前二相对全市场、板块龙一/龙二相对板块、竞价确认增量、09:50 和 13:15 确认增量。未通过的子信号保持研究态，不能只因组合分提高就注册实盘。

反身性护栏另做逐层消融：基线、基线+市场阶段、基线+玩家证据、基线+玩家交互。
报告至少包含成本后 T+1/T+3 期望、最差 5% 收益、最大回撤、炸板率、次日低开率、
终态覆盖率和不可交易样本；只有收益风险比在冻结测试集上改善，且不是靠无限 pending
排除困难样本，才允许进入下一 promotion 阶段。

报告复用 `candidate_lifecycle` 的全候选 T+1/T+3 结算，不另建收益账本。它对比“全额
追入”与当时反身性仓位倍率，分别输出成本后均值、最差样本、下行波动、盈亏比、各护栏
错杀盈利样本比例，并显式统计 unresolved、缺失收益和未知反身性样本。没有足够结算样本
时状态必须为 `insufficient_data`，不得用合成数据或零值冒充策略有效。
