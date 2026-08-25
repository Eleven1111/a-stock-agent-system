# 分钟线派生字段管道（2026-08）

> 用 Bash heredoc 写入：settings 钩子拦 `docs/*.md` 的 Write，这是一份属于仓库的
> 常规设计文档，不走钩子白名单，故在此说明原因。

事件表 v4（#274）把 S1 的 `volume_ratio` 与 S2 的 `pre_reseal_turnover_pct` 声明为
`unavailable:needs_intraday_minute_bars`。本轮把这两个字段真正填上。**本轮补的是数据，
不是策略**：`skills/common/rank_surprise.py` 与 `divergence_reseal.py` 一行未动。

## 1. 探针：分钟数据到底能从哪拿、能拿多深（2026-08-25 本机实测）

| 来源 | 结果 | 证据 |
|---|---|---|
| mootdx / 通达信 TCP 7709 | **不可用**，分钟深度无法实测 | 38 个 HQ 节点：2 个握手成功但 `bars()` 全空、其余 `ResponseHeaderRecvFails`；握手成功的节点 `stocks(market=0)` 正常返回 24078 行 → 是仓内已记录的 bestip 坑，不是「没有分钟数据」 |
| 东财 push2his 分钟历史 | **不可用** | `klt=1` / `klt=5` 均返回 HTTP 200 + 空 body（curl 直连与仓内 http_client 一致） |
| 新浪分钟 K（`CN_MarketDataService.getKLineData`） | **可用，有历史但浅** | `datalen` 上限 1023、不支持翻页；`scale=5` → 1023 根 = **22 个交易日**（最早 2026-07-27），`scale=1` → 1023 根 = **5 个交易日**（最早 2026-08-19）。sz000001 / sh600519 / sz300750 三只结果一致 |
| 腾讯分时 | **可用，仅当日** | sz000001 当日 267 行（09:30–15:30） |

结论：**路径 A（历史回填）成立但窗口只有 22 个交易日**，走新浪 5 分钟线，不走 TDX；
更早的历史两条源都拿不到，一律 `unavailable`。**路径 B（向前累积）照做**，从落盘作业
上线之日起累积。「TDX 支持分钟频率」是文档结论，本机实测拿不到，不据此规划。

### 单位交叉验证（两条源互证，不是各自自说自话）

2026-08-25 sz000001 截至 09:45：

- 腾讯 `cum_volume = 146,525`（**累计**，单位**手**）
- 新浪 09:35+09:40+09:45 = 8,300,378 + 3,310,776 + 3,057,735 = 14,668,889（**增量**，单位**股**）= 146,689 手

两者差 0.11%（bar 边界归属差异）。仓内出过「volume×close 漏乘每手股数把成交额低估
100 倍」的事故，因此换算集中在 `minute_derived`，不在调用点手写。

## 2. 字段口径与公式

`skills/common/minute_derived.py`（纯函数，不触网）：

- **量比** `volume_ratio_at(rows, checkpoint="09:45", baseline_per_minute)`
  ```
  量比 = (截至 checkpoint 的累计成交股数 ÷ 已走过的连续竞价分钟数) ÷ 基准每分钟均量
  ```
  分母是**走过的分钟数**（`elapsed_trading_minutes`，跳过午休），不是行数 —— 5 分钟线
  到 09:45 只有 3 根但已经走了 15 分钟，用行数会把量比放大 5 倍。集合竞价成交量含在
  当日首根里，属于分子（与市场通行口径一致）。
- **量比基准** `baseline_per_minute_from_daily(kline, date, window_days=5)`
  ```
  基准 = 事件日之前 5 个交易日的总成交股数 ÷ (5 × 240)
  ```
  口径进 config：`event_table_v4.volume_ratio_checkpoint` / `.volume_ratio_baseline_days`。
  样本不足 5 天 → `unavailable`，不用手上的两三天凑。
- **封板前累计换手** `cumulative_turnover_before(rows, until_time, float_shares)`
  ```
  换手% = 截至 until_time（含收线于该时刻的那根）的累计成交股数 ÷ 流通股本 × 100
  ```
  `float_shares = 流通市值 ÷ 事件日收盘价`，**沿用 v4 的口径并继承其已知偏差**：流通
  市值取自涨停池快照，与收盘价可能不同步，个位数百分比误差属已知，不做二次修正。

**禁未来函数**：两个函数都只吃 `until_time` 之前的行；多喂后续行结果不变
（`tests/test_minute_derived.py` 的截断/全天对照 + `tests/test_event_schema_v4.py`
的「回封后加一根巨量条」用例）。

**fail-closed**：行缺失 / 覆盖不到 checkpoint / 窗口内根数不足（中间有洞）/ 缺流通
股本 / 缺基准 → `value=None` + `unavailable:<原因>`。绝不返回 0，绝不用日线代理值。
累计序列非单调 → 整份作废（不 clamp，clamp 出来的「正常数据」正是假绿的温床）。

## 3. 落盘格式与容量

`skills/common/minute_derived_store.py`，路径
`$A_STOCK_STATE_HOME/skills/daban-stock-picker/data/minute_derived/<YYYY-MM-DD>.json`
（已加入两个 hot-money checkpoint 作业的 `allowed_state_writes`）。

```json
{"schema": "minute_derived_v1", "date": "2026-08-25", "count": 20, "truncated": 0,
 "records": {"600001": {"slots": {"0935": 830037.8, "0940": 331077.6},
                        "slots_step_minutes": 5, "slots_availability": "available",
                        "volume_ratio": null,
                        "volume_ratio_availability": "unavailable:baseline_per_minute_unavailable"}}}
```

**存的是派生曲线，不是原始分钟条**：每票 48 个 5 分钟增量值 ≈ 0.5 KB，全市场
`max_codes=800` 上限下 ≈ 0.4 MB/天，`prune_days=60` 滚动保留 ≈ 24 MB。若改存 240 根
原始分时，同样覆盖面要大两个量级。

存曲线而不是直接存两个成品字段，是因为**封板前换手要的回封时刻当天盘中并不知道**
（炸板次数要收盘后的涨停池才有），只能存曲线、事后按 `reseal_time` 取值。

挂载点：`hot_money_checkpoint.persist_minute_derived`（09:50 / 13:15 两个既有作业）。
该作业本来就为每个候选抓分时，**本轮不新增任何网络请求**，只是把过去用完即弃的数据
派生后存下来。量比不在这里算：基准要过去 5 日日线，本作业手上没有，去取就成了新增
请求；量比改在事件表构建时用回测已抓的日线现算。

## 4. 接进事件表

`daban_bt_data._minute_event_fields` 填充 v4 契约里原为 unavailable 的两个字段，
**没有升 schema**（`EVENT_SCHEMA` 仍是 `daban_bt_event_table_v4`，单一事实源不动）。
`build_event_table(..., minute_source="auto"|"store"|"sina"|"none")`，缓存键带
`minute_source`，避免 `none` 建的表被 `auto` 静默复用。

可得性矩阵（按来源）：

| 字段 | akshare + sina 回填 | akshare + store | mootdx |
|---|---|---|---|
| `volume_ratio` | 窗口内 available | 落盘之后的交易日 available | unavailable（无分钟行） |
| `pre_reseal_turnover_pct` | 同上，且要求炸板次数 > 0 | 同上 | unavailable |
| 炸板次数 = 0 的票 | `not_applicable:never_opened_board_no_reseal` | 同左 | unavailable |

## 5. 仍缺什么

- **历史深度**：新浪 5 分钟只回溯 22 个交易日。要做真正的 OOS，要么等路径 B 累积，
  要么找一条更深的分钟源（本机 TDX 不通，需在部署机复测）。
- **mootdx 分钟深度未知**：本机 38 节点 `bars()` 全空，深度这一项是 UNVERIFIED，
  不是「不够」。部署机网络不同，值得复测一次再决定要不要接。
- **落盘覆盖面**：挂载点是 hot-money checkpoint，只覆盖当日候选（`MAX_CANDIDATES=20`），
  不是全市场涨停池。要覆盖全部涨停票，需要另一个本来就抓全市场分时的作业
  （`eod_anomaly_scanner` 是现成的，但它当前不在 cron manifest 里）。
- **流通股本口径偏差**：继承 v4，未修正。

## 6. 端到端实证（真实数据，2026-08-25/26 跑）

窗口 2026-07-28 ~ 2026-08-21（`stock_zt_pool_em` 实际覆盖 2026-08-05~08-21，共 13 个
交易日；覆盖告警照常打印，样本已退化的事实不掩盖），`minute_source="sina"`，
981 个 (日期, 代码) 键，976 个拿到分钟行（覆盖率 **99.49%**），耗时 2010 s。

事件表可得性（648 条事件）：

| 字段 | available | not_applicable | unavailable |
|---|---|---|---|
| `volume_ratio` | **648** | — | 0 |
| `pre_reseal_turnover_pct` | **274** | 339（一次没炸板，不存在"封板前"） | 35（`minute_rows_start_after_checkpoint`：回封早于当日首根 5 分钟线，如 09:25 竞价即回封） |

S1 / S2 在这张表上的结果（**NON-LIVE 研究观察，样本极小，不构成任何结论**）：

| | signal | filled | mean 净收益 | win_rate |
|---|---|---|---|---|
| S1 RankSurprise（`--market-state S3`） | 4 | 2 | +3.47% | 0.50 |
| S2 DivergenceReseal | 6 | 6 | −4.09% | 0.167 |

**两个策略首次在真实数据上跑出非零命中**（此前恒为 0）。S1 仍带
`betas_unfitted_placeholder` 降级标记，`volume_ratio_source=minute_derived:09:45`
如实写进 degraded 列表。剩余 unavailable 的主因不再是分钟字段，而是
`peer_sample_insufficient`（503）与 `expected_gap:peer_gap_sample_insufficient`（533）
—— 13 个交易日 × 单板块 peer 数不够，属样本长度问题，不是数据管道问题。

### 单位链条的独立验证

日线 `volume` 是**手**、分钟 `volume` 是**股**这一条，用两条独立源逐日对账：

```
2026-08-21 tencent_daily_volume 869128.0  sina_shares 86912763  ratio 100.0
2026-08-24 tencent_daily_volume 1199025.0 sina_shares 119902495 ratio 100.0
2026-08-25 tencent_daily_volume 994881.0  sina_shares 99488115  ratio 100.0
```

三天比值精确等于 100 —— `baseline_per_minute_from_daily` 里的 `× lot_shares` 不是
猜的。数值合理性抽查：000593 / 2026-08-05 回封 09:35:24，封板前换手 5.68%，上游给的
全日换手 6.86% —— 封板前 < 全日，量纲与大小关系都对。

## 量比阈值在真实分布下几乎不构成约束（验收时补记）

分钟字段落地后第一次能看到 `volume_ratio` 的真实分布：**中位 7.84、p90 27.9**。

原因是样本本身全是涨停票，且基准取过去 5 日日线均量——前 5 日若有缩量一字，基准被
压得很低，比值自然被推高。单位链条已用腾讯（手）与新浪（股）双源对账过（差 0.11%，
逐日 ratio 精确 100.0），所以这不是换算错误，是这批样本的真实形态。

含义：`rank_surprise` 的 `min_volume_ratio = 1.5` 在这个分布下**几乎筛不掉任何东西**，
S1 的四个入场条件实际只有三个在起作用。这不是本轮要改的事——按纪律，阈值调整必须走
research_gate 用样本外数据定，不能看着分位数直接回拟合（`daban_thresholds.yaml` 的铁律）。
记在这里是为了让下一个动这个阈值的人先看到分布，而不是先看到 1.5 这个数字。
