# 尾盘主线续强策略协议

本协议冻结 `tail_close` V1 的研究边界、插件接口和预注册初稿。当前结论仅为
`READY FOR RESEARCH-ONLY IMPLEMENTATION`：没有 OOS 或前瞻 shadow 结果，不声称策略
有效或有收益，也不授权连接券商或自动下单。唯一配置源是
`config/tail_close_strategy.json`。

## 策略身份与隔离

| 策略 | 决策与成交 session | 当前状态 |
|---|---|---|
| `tail_close:mainline_continuation_v1` | 14:50 连续竞价，14:50:20–14:56:30 模拟限价成交 | `research_only`，`live_weight=0` |
| `tail_close:after_hours_fixed_v1` | 15:05–15:30 盘后固定价格 | `not_ready`、禁用、独立后续 sibling |

两者不得共享 config hash、成交/队列模型、OOS、shadow 或 promotion 结论。14:50
未成交余量不得进入 14:57 收盘集合竞价，也不得转入 15:05 sibling。15:05 sibling
只冻结事前信号和模拟申报，15:31 才允许读取 15:30 前的增量队列事实并对账；
后验成交结果不能反向改写 15:05 信号。在独立数据能力审计、独立 OOS 和独立
前瞻 shadow 完成前保持禁用。

14:50 主策略也不得使用打板/趋势策略 ID、早盘 gap/封单/开盘动作，或依赖 15:00
EOD、15:05 sibling、15:07 candidate discovery 的产物。

## 固定时钟与 PIT

```text
14:34:59  prepare_cutoff
14:35:00  prepare；只准备静态、可重验状态，不产生信号
14:49:59  decision_cutoff；event_time 和 available_time 均不得越界
14:50:00  decision；补齐增量并重算全部时变门
14:50:20  deadline；之后产生的信号为 NO_ACTION_LATE
14:56:30  cancel；取消模拟未成交余量
14:57:00  不再假设连续竞价成交
```

每条源数据和派生特征必须记录 `event_time`、`available_time`、source
版本、snapshot/feature/config hash 和 code version。首次 sealed snapshot 是重试的
唯一输入；hash 不一致、水位不足、时钟漂移超限、晚到数据或封存失败均 fail-closed。
R0 允许的最大源时钟偏移为 2 秒。
盘后修订只能新增 replay 记录，不能覆盖实时 shadow 事实。

## R0 规则

R0 只检验“点时主线续强”，其他形态是固定对照臂，不得在看完 OOS 后并入主策略。

股票池仅含沪深主板普通 A 股，排除 ST、退市整理和上市不足 120 个交易日的证券。
过去 20 个交易日中位成交额至少 2 亿元，截止 14:49:59 当日成交额至少 3 亿元。
任何证券状态、公告风险、交易状态或数据字段不能证明为点时可得时，拒绝而不是回填
中性值。

市场门在 `risk_off`、未知、过期或数据质量失败时输出 `NO_ACTION`。R0 初值要求
14:00–14:49 宽基收益不低于 -0.8%，上涨家数占比不低于 35%；无清晰主线允许空池。

主线截面分冻结为：

```text
0.30 × session relative return
+ 0.25 × 14:00-cutoff relative return
+ 0.20 × breadth
+ 0.15 × persistence
+ 0.10 × liquidity support
```

每个板块至少有 3 只点时有效成分股、breadth 不低于 0.55，只保留截面前 20%。
并列依次按有效成分数、PIT 成交额和稳定板块 ID 排序。板块成员、停牌/ST 状态、
收益、持续性和成交额都必须是 PIT 版本；收盘字段和盘后标签禁止进入特征。

个股需同时满足：当日涨幅 2%–6%、价格高于 VWAP、日内位置不低于 0.80、
`MA5 > MA10 >= MA20` 且价格高于 MA20、14:30–14:49 至少 70% 有效分钟在
VWAP 上方。放量急跌、单分钟脉冲、不可买涨停、临近跌停、无卖盘、报价异常或
高开低走且仍低于开盘价均拒绝。

可执行的连续性初值为：尾盘至少 15 个有效分钟，单分钟对尾盘总涨幅的贡献不超过
50%，最近 10 分钟收益不低于 -1%；若任一分钟跌幅不高于 -0.5% 且该分钟成交量
达到尾盘分钟成交量中位数的 2 倍，则定义为放量急跌并拒绝。这些是待 OOS 证伪的
冻结 R0 阈值，不是已验证最优值。

硬门通过后才按以下权重确定性排名：

```text
0.30 × mainline strength
+ 0.25 × within-sector leadership
+ 0.20 × price continuity
+ 0.15 × volume continuity and non-pulse quality
+ 0.10 × execution capacity
```

最多保留 10 只研究候选、3 条研究信号、每个板块 1 只。并列按可执行性、PIT
成交额、证券代码排序；不为凑数补位。每个分量必须保留原值、截面位置和 lineage。

## 插件协议

`tail_close_strategy_plugin_v1` 是无副作用纯接口：

```text
prepare(context, as_known_inputs) -> tail_close_prepared_state_v1
gate(prepared_state, sealed_snapshot) -> tail_close_gate_result_v1
rank(gate_result) -> tail_close_rank_result_v1
simulate_execution(admitted_signals, execution_evidence)
  -> tail_close_simulated_execution_v1
label_outcome(simulated_fills, d1_as_known_evidence) -> tail_close_outcome_v1
```

### `prepare`

输出至少含 strategy/run/batch/trading-date/session 身份、配置和代码版本、输入 refs、
source watermarks、静态证券池和准备状态。它只能使用 `prepare_cutoff` 前的
as-known 数据，不产生最终候选、信号、订单意图或成交，也不能把 14:35 时变状态
直接带入 14:50。

### `gate`

输入必须是本 batch 的 prepared state 和首次 sealed 14:50 snapshot。输出包含
snapshot/feature/config hash、clock/watermark 检查、市场/板块/证券/公告/
可交易性门、逐项拒绝原因和状态。所有时变特征必须重算；缺失、越界或 hash 不一致
为 `FAIL_CLOSED`，正常空池为 `NO_ACTION`。

### `rank`

只排序 `gate` 已放行的证券，输出原始特征、截面位置、lineage、确定性
tie-break 和 rank hash。它不能增加候选、修改 policy 或分配组合容量。

### `simulate_execution`

只消费 shared `decision_policy` 和 `portfolio_policy` 已准入的研究信号。它生成
`simulated_order_intent` 与 `FULL_FILL/PARTIAL_FILL/UNFILLED`，使用模拟到达时间
之后的盘口/成交证据、限价、价差、队列折扣、可见量、共享容量和费用。仅实际模拟
成交量计算收益和资金占用；收盘价不能代替入场价。

### `label_outcome`

主标签为下一交易日 09:35–09:40 TWAP。停牌、跌停或流动性不足时保留到首次
可执行窗口，继续计入 `days_blocked`、`capital_days` 和尾损；最多观察 5 个交易日，
之后 right-censor 并保留保守尾损。不得删除受阻或删失样本，也不得在 T 日退出。

### 权限边界

插件不得：

- 写入或晋级 `strategy_registry`；
- 绕过 shared `decision_policy` 或 `portfolio_policy`；
- 创建第二套 ledger、audit、validation 或真实持仓状态；
- 直接写统一 ledger/audit/validation（由 shared runtime 负责）；
- 导入、构建或调用 broker/order client；
- 创建、发送、撤销或修改真实订单。

shared runtime 的顺序固定为：

```text
prepare -> sealed snapshot -> gate -> rank
-> decision policy -> portfolio policy
-> simulate execution -> unified ledger/audit
-> label outcome -> unified validation
```

因此插件即使返回高排名，也不能自行获得组合准入、live 权重或账本事实。

自动模拟对账必须与 order/fill 一起追加到统一 `signal_ledger.jsonl`，事件类型为
`tail_close.simulation_reconciled`，并绑定 `decision_hash` 与 `fill_hash`。
`recommendation_audit` 只从该账本重建独立的尾盘研究生命周期视图；它不写第二份
事实账本，也不把尾盘研究事件投影为可结算的实盘信号。缺任一阶段、链接不一致或
hash 不一致都形成 audit violation。

## 模拟成交、退出和组合

信号与成交分离。模拟订单使用 100 股整数手、有价格上限的限价买入，并区分全部、
部分和零成交。参与量、单票共享容量、组合分配和集中度取最小约束；报告 5/10/20
bps 冲击压力。R0 的已观察系统延迟和人工复核延迟均冻结为 0 秒，单信号研究名义
金额为 10 万元，限价相对信号价的最大溢价为 0.003。该值是小数比例（30 bps），
不是百分数，消费者不得再次除以 100。上述初值只用于形成可复验的基线，不代表真实
shadow 延迟。盘后 sibling 的队列折扣初值为 0.5，仍因数据能力 `not_ready` 而
禁用。费用版本复用 `skills/common/execution_model.py`，不在插件中复制。

D1 指下一交易日。主退出只有 09:35–09:40 TWAP；09:30 首个可执行价、10:00 和
收盘仅为固定敏感性标签，不参与事后择优。

同日同股跨策略去重，已有持仓先占用证券和组合容量，早盘与尾盘共享证券、板块、
主题和组合上限。报告同时给出尾盘 standalone 结果及加入现有策略后的 incremental
结果；incremental 不稳定为正时不能晋级。

## 预注册、停止规则和晋级门

配置中 `validation.precommit` 是 R0 预注册初稿。主指标为计入费用、冲击、容量、
未成交、退出受阻和删失后的组合增量净期望；主退出为 D1 09:35–09:40 TWAP。
20 日新高、VWAP 回收和逆指数相对强势是固定对照臂，不能事后挑冠军。

历史 OOS 至少覆盖 3 年和 500 笔原始模拟成交，使用 walk-forward、
purge/embargo 和多重比较校正。除了仓库统一统计门，还需同时满足：

- precommit 与 reveal 必须来自共享、持久化、append-only 的 `OOSRegistry`；
- reveal 必须是独立 invocation，且绑定当时冻结的数据集文件 SHA-256；
- 只有 `exited + observation_complete=true + non-censored` 样本可进入统计；
- pending、right-censored、重复或身份缺失样本必须守恒记录并使晋级 fail-closed；

- 净收益均值置信区间下界大于 0；
- 利润因子至少 1.20；
- 20 bps 冲击压力下净期望不为负；
- 收益不由单月、单板块或少量样本主导；
- 受阻、删失和尾损在预注册预算内；
- 与早盘策略组合后的 incremental 期望为正。

R0 最大 right-censored 比例为 5%。

失败则保持 research-only，形成失败报告；不得追加因子延长同一次检验，下一假设
必须新建 precommit。

只有 OOS 通过后才能开始至少 60 个真实交易日的前瞻 shadow。shadow 每日先冻结
14:50 信号，再盘后重放；PIT、SLA、ledger/audit 和幂等重大事故必须为 0，成交、
滑点、退出和资金占用误差需在预注册容差内，其中 fill-rate 最大误差为 2%。全程
仍为 `live_weight=0` 和零订单。

manual pilot 还必须单独通过 OOS、shadow 和人类明确审批。届时仍由人复核、人决定、
人下单；系统只追加 simulated-vs-actual reconciliation。该入口默认关闭；开启后
也只能读取持久化的 OOS、shadow、人工审批产物及外部成交凭证文件，现场复算文件
SHA-256，并要求外部 broker 凭证已确认。自声明 hash、未确认凭证或与模拟 fill 不
一致的记录全部 fail-closed，并由统一账本审计视图报告。账户成本、行情能力和用户
风险预算未冻结前，状态固定为 `not_eligible`。任何 PIT/hash 违规、净期望转负、
组合增量转负或无法解释的 shadow 漂移都立即停止晋级并退回研究。

## 永久安全断言

配置和运行时必须同时满足：

```text
research_only = true
live_weight = 0
broker_access = forbidden
broker_call_count = 0
automatic_ordering = forbidden
automatic_order_count = 0
```

这些字段不是“当前暂定值”，而是本研究实现的权限上限。未来 manual pilot 也不能
在此仓库中增加 broker 或自动下单能力。

## 验证

```bash
pytest -q tests/test_tail_close_contract.py tests/test_config_registry.py
python -m ruff check skills/common/config_registry.py tests/test_tail_close_contract.py
git diff --check
```

静态测试只能证明合约和安全边界存在，不能代替真实 point-in-time OOS 或 shadow。
