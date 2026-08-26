# P4 — 仓位管理、四层止损与熔断阶梯（paper 先行）

> 来源：`docs/hot-money-emotion-system-upgrade-plan-2026-08.md` §7。
> 实现：`skills/common/position_risk.py`、`skills/common/exit_signals.py`、
> `skills/common/decision_policy.py`（默认关闭的开关）、`skills/common/paper_trading.py`。
> 状态：**paper 先行**。实盘链路的既有行为一字未改，见文末「paper-only 边界」。

---

## 0. 一句话

仓位不再由「我觉得这次胜率高」决定，而由**愿意亏多少**（RiskBudget）÷ **止损多远**
（StopDistance）决定；加仓不再是摊低成本，而是**逻辑确认 → 盈利确认**的单向阶梯；
退出不再只看价格，而是市场/题材/龙头/个股四层，且**事件止损优先于价格止损**。

---

## 1. 1+1+1 加仓状态机

```
                    ┌──────────────┐
   策略信号成立  →   │  logic_leg   │  +10%      （position_pct 10%）
                    └──────┬───────┘
                           │  板块确认 ∧ 龙头确认 ∧ 市场未恶化
                           ▼
                    ┌──────────────┐
                    │ confirm_leg  │  +10%      （position_pct 20%）
                    └──────┬───────┘
                           │  已有浮盈 ∧ 逻辑仍成立
                           ▼
                    ┌──────────────┐
                    │  profit_leg  │  +10%      （position_pct 30% = 单股上限）
                    └──────────────┘

   任一时刻浮亏（unrealized_pnl_pct < 0）且请求 confirm/profit
                           │
                           ▼
                    ┌──────────────┐
                    │   LOCKED     │  confirm/profit **永久**关闭
                    └──────────────┘   （之后转盈也不再放行）
```

**这不是摊低成本**。原书的 1+1+1 是「逻辑确认加仓 + 盈利确认加仓」；亏损加仓是明确
禁令，不是建议。因此 `locked` 是永久状态而不是「本次跳过」——实现里
`apply_leg` 即使在拒绝的分支上也会返回带 `locked=True` 的新状态。

被守住的四类非法转移（单测见 `tests/test_position_risk.py`）：

| 非法转移 | 拒绝理由码 |
|---|---|
| 浮亏时加 confirm/profit | `losing_add_forbidden`（并永久上锁） |
| 跳级（logic → profit，跳过 confirm） | `leg_out_of_order` |
| 重复落同一条腿 | `leg_already_filled` |
| 超单股上限 30% | `single_position_cap_exceeded` |
| 缺浮盈数据 | `unrealized_pnl_unavailable`（拒绝本次，**不**上锁） |

每条腿——**成功与被拒都落账**——写一条 `position.ladder_leg` 事件，幂等键
`position.ladder_leg:{signal_id}:{leg}:{filled|blocked}`。只记成功的话，「今天为什么
没加仓」在回放里查不到。

---

## 2. R 化风险预算

```
Position = min( ModeCap , RiskBudget / StopDistance )

RiskBudget  = 账户净值 × 0.5%–1.0%      （config/paper_trading.json → position_risk.risk_budget_pct）
StopDistance= 策略结构止损  或  1.2–2.0 × ATR(14)，夹进 3%–8% 研究区间
```

单位换算（实现里就是这一行）：`仓位% = RiskBudget% / StopDistance% × 100`。

### 算例

| # | NAV | RiskBudget% | StopDistance% | ModeCap% | 风险预算仓位 | **最终仓位** | 约束方 |
|---|---|---|---|---|---|---|---|
| A | 100,000 | 1.0 | 5.0 | 30 | 1.0/5.0×100 = 20% → 20,000 元 | **20%** | `risk_budget` |
| B | 200,000 | 1.0 | 3.0 | 30 | 33.33% → 66,667 元 | **30%**（60,000 元） | `mode_cap` |
| C | 100,000 | 0.75 | 5.0 | 30 | 15% → 15,000 元 | **15%** | `risk_budget` |
| D | 100,000 | 1.0 | **0 / 缺失** | 30 | — | **0%（blocked）** | `fail_closed` |

算例 A 逐步：RiskBudget = 100,000 × 1.0% = **1,000 元**；止损 5% 意味着 1,000 元的
亏损对应 1,000/0.05 = **20,000 元**头寸 = NAV 的 20%。

**为什么要 R 化**：它替代的是「我觉得这次胜率 90% 所以半仓」。交易者无法可靠知道
自己的真实条件胜率——那个数字是事后编的。R 化把「信心」这个不可观测量换成了两个
可观测量（愿意亏多少、止损在哪）。

**fail-closed 是硬要求**：`StopDistance ≤ 0` 时除法会得到无穷仓位。实现返回
`status=blocked` + `position_pct=0.0`，NAV ≤ 0 与 ModeCap ≤ 0 同理。「没有止损」在
本模块里等价于「不许开仓」，不等价于「不设上限」。

---

## 3. 环境总仓表

替换 `decision_policy` 里「S6 压到 20%」的单点规则为 S0–S6 全状态表。五档温度经
`TIER_TO_STATE` 折进同一张表，不维护第二份阈值。

| 状态 | 语义 | 五档温度 | 总仓区间 | 倍率（上限/100） |
|---|---|---|---|---|
| S0 | 冰点未确认 | 冰点 | 0–10% | 0.10 |
| S1 | 萌芽确认（修复） | 修复 | 20–40% | 0.40 |
| S2 | 发酵（点火） | 发酵 | 40–70% | 0.70 |
| S3 | 加速（扩散/主升） | 加速 | 30–60% | 0.60 |
| S4 | 高潮（拥挤） | 极热 | 0–30% | 0.30 |
| S5 | 分歧转一致（轮动） | — | 40–70% | 0.70 |
| S6 | 退潮确认（级联） | — | 0–10% | 0.10 |
| 未知 | 状态不可用 | — | **0%** | 0.00（fail-closed） |

两处值得注意：**加速档（S3）低于发酵档（S2）**——加速是晚期，离高潮更近；
**未知状态归零**——不知道环境是什么的那天，恰好是最不该按中性仓位下注的那天。

验收方式是**行为断言**：构造退潮日/高潮日/发酵日 mock，断言
`evaluate_decision` 对正向建议**实际输出的** `position_multiplier × 100` 落在表内区间，
而不是去读配置字段的值（读字段只能证明配置写对了）。见
`tests/test_position_risk_decision_policy.py`。

---

## 4. 四层止损与事件止损优先

```
优先级从上到下；同一 severity 内，事件类信号排在价格类之前。

  ┌ 市场层  sentiment_exit（温度计退潮 + 板块连板断裂）、flow_reversal
  │
  ├ 题材层  theme_invalid   ← 新增
  │           题材分跌幅 ≥ 20   或   主线降为后排 ∧ 助攻掉队 ≥ 50%
  │
  ├ 龙头层  leader_invalid  ← 新增        lhb_climax（既有）
  │           LeaderScore 跌幅 ≥ 20   或   龙头断板 ∧ 承接断层
  │         event_stop      ← 新增（可越过价格止损）
  │           龙头大幅低开(≤ −3%) ∧ 助攻无溢价(≤ 0) ∧ 昨日后排跌停
  │
  └ 个股层  stop_loss / trailing_stop / take_profit / time_stop /
            catalyst_negated / deep_research_exit
```

**事件止损优先于价格止损**：价格止损在情绪股上天然滞后。龙头低开、助攻无溢价、
昨日后排跌停已经把承接打穿时，ATR 还没触及——但那一口承接明天不会回来。实现上
`evaluate_all_exit_signals` 的排序键是 `(severity, 事件类优先)`，且 `sort` 稳定，
因此**既有信号之间的相对次序一字未动**。

三条新信号的「且」条件都是刻意的：单看排名下滑是题材轮动的常态、单看断板可能只是
换手。`event_stop` 三个条件缺任一即不触发——这条规则强到可以越过价格止损，不能让
缺数据把它推成默认成立。

paper 层的接线：`simulate_exit_checks(exit_overrides={code: reason})`，排在
`pending_exit`（已登记的处置计划）之后、`_exit_reason`（价格止损）之前。不传时行为
与改造前完全一致。

### T+1 × 止损冲突

触发退出但当日 T+1 锁定 → **只记录 + 次日处置计划**，仓位不动、现金不动：

```
D 日  事件止损触发 → t1_constraint.sell_allowed = False
      → 事件 status=pending_t1，position.pending_exit =
        {reason, triggered_on: D, earliest_sell_date: D+1}
      → cash / shares / realized_pnl 全部不变
D+1   不重算信号，按已登记的计划执行 → status=filled，reason 一路带到成交记录
```

见 `tests/test_position_risk_t1_integration.py`（走真实 `paper_trading` +
真实手续费模型，不是 stub）。

---

## 5. 熔断阶梯（R 化）

| 档位 | 触发条件 | 倍率 | 停新开 | 其他 |
|---|---|---|---|---|
| `day_loss_2r` | 单日 ≤ −2R | 0.0 | ✅ | |
| `week_loss_reduce` | 单周 ≤ −4R | 0.5 | | 降仓 |
| `week_loss_freeze` | 单周 ≤ −5R | 0.0 | ✅ | |
| `drawdown_halve` | 账户回撤 ≥ 8% | 0.5 | | 仓位减半 |
| `drawdown_stop` | 账户回撤 ≥ 10% | 0.0 | ✅ | **停实盘 + 强制复盘周** |
| `theme_risk_cap:<主题>` | 同主题在险 > 2R | 0.0 | | 只封该主题 |
| `off_system_streak` | 连续 ≥ 3 笔系统外交易 | 0.0 | ✅ | 强制停手 |

1R = 一次预设止损的亏损额。R 化的意义：−2R 在不同净值下是不同金额，却是同一个
「两次预设止损」。

**每一档（触发与未触发）都返回并落账**，一档一条 `risk.circuit_rung` 事件，幂等键
`risk.circuit_rung:{asof}:{rung}`，因此「当天熔断状态」可逐档回放对账。缺数据的档
标 `observed=None` 而不是 0——「今天没亏」和「今天没数据」必须可区分。

### 与 discipline_score / behavior_risk 的合并

`merge_position_multipliers(...)` 取**更保守（最小）**的倍率，**绝不相乘**：

```
熔断 0.5  ×  纪律分 0.5  =  0.25   ← 错：同一个坏日子被两套独立口径各罚一次
min(熔断 0.5, 纪律分 0.5) = 0.5    ← 对
```

三者口径互不重叠：熔断看**盈亏与回撤**，`discipline_score` 看**单日执行偏差**，
`behavior_risk` 看**跨日行为形态**。与 `discipline_score.combined_position_multiplier`
的口径完全一致（同样取小不相乘），本模块只是把入参扩展到任意多路。

---

## 6. paper-only 边界（重要）

本轮**没有改变实盘链路的任何既有行为**。具体地：

| 改动 | 生效范围 | 保证方式 |
|---|---|---|
| `position_risk.py` 全部函数 | 纯函数库，无自动接线 | 目前调用方只有 paper / 研究路径与测试 |
| 环境总仓表接进 `decision_policy` | **默认关闭** | `HERMES_ENV_POSITION_TABLE` 不等于字面量 `enforce` 时输出与改造前**逐字段一致**（`test_output_is_field_for_field_identical_when_flag_absent` 覆盖全部 7 个状态 + 未知 + 缺失） |
| `exit_signals` 三条新信号 | 默认不触发 | 新参数全部默认 `None`/`False`；不传时三条信号一律 `triggered=False` |
| 事件止损排序 | 只影响新信号 | `EVENT_STOP_SIGNALS` 仅含新增三类，`sort` 稳定 → 既有信号相对次序不变 |
| `simulate_exit_checks(exit_overrides=…)` | 默认 `None` | 不传时与改造前完全一致（有对照断言） |
| `config/daban_thresholds.yaml` | **新增节 `circuit_ladder_r`** | 既有阈值（含 `market_gate`）**零删改**，git diff 只有新增行 |
| `config/paper_trading.json` | 新增 `position_risk` 节，`enabled: false` | `required_roots` 是子集校验，新增键不影响既有加载 |

`market_gate` 的百分比口径与 `circuit_ladder_r` 的 R 口径**并存是刻意的**：R 口径要
先在 paper 跑出结算样本，证明它比百分比口径更早拦住坏日子，才谈替换 `market_gate`。

### 尚未满足的验收项

方案 §7.2 第三条要求「熔断阶梯在 paper trading 连续运行 ≥ 20 笔真实结算样本」。
本轮**不满足**——本机无结算样本（见 MEMORY「本机只做管道就绪」）。本轮交付的是
**管道就绪 + 单测/集成断言**，不是「熔断阶梯已验证有效」。打开
`HERMES_ENV_POSITION_TABLE=enforce` 或把 R 口径接进实盘之前，必须先补齐这条。
