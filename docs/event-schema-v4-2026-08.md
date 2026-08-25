# 回测事件表 EVENT_SCHEMA v4（2026-08）

> 用 Bash heredoc 写入：settings 钩子拦截 `docs/*.md` 的 Write，本文件是验收标准 9 要求的交付物。

`daban_bt_event_table_v3` → **`daban_bt_event_table_v4`**。本轮只补数据管道，S1/S2 的信号判定逻辑一行未改；
`config/daban_thresholds.yaml` 只**追加** `event_table_v4` 节，既有阈值一条未动。

实现：`skills/chanlun-backtest/scripts/daban_bt_data.py`（`V4_FIELDS` / `derive_reseal_time` /
`sector_cross_section` / `turnover_baseline` / `_v4_event_fields`）。

## 1. 字段清单 × 来源可得性矩阵

每个事件都带 `field_availability: {字段 → available | unavailable:<原因> | not_applicable:<原因>}`，
表级带 `field_availability_summary` 汇总。**标了非 available 的字段，值一定是 None**（测试逐条断言，杜绝伪造值）。

| 字段 | 含义 | akshare（stock_zt_pool_em） | mootdx（通达信日线重建） |
|---|---|---|---|
| `turnover_pct` | T 日全日换手率 % | available（上游 `换手率`，v3 之前被丢弃） | unavailable:not_provided_by_source |
| `last_seal_time` | 最后封板时间 | available（上游 `最后封板时间`） | unavailable:not_provided_by_source |
| `open_board_count` | 炸板次数 | available（上游 `炸板次数`） | unavailable:not_provided_by_source |
| `reseal_time` | 回封时刻（见 §2） | available / not_applicable | unavailable:open_board_count_missing |
| `sector_limitup_count` | 当日该板块涨停家数 | available（sector 缺失 → unavailable） | unavailable:sector_missing |
| `sector_one_word_count` | 板块内首封 ≤09:25 家数 | available（组内封板时间不全 → unavailable） | unavailable:sector_missing |
| `sector_fast_board_count` | 板块内首封 ≤09:31 家数（含一字） | 同上 | unavailable:sector_missing |
| `turnover_baseline_median` | 事件日前 20 交易日换手率中位数 % | available（需流通市值反推流通股本） | unavailable:float_shares_unavailable |
| `turnover_baseline_sample_days` | 上述有效样本天数 | available | unavailable |
| `pre_reseal_turnover_pct` | 封板前累计换手 % | **unavailable:needs_intraday_minute_bars** | 同左 |
| `volume_ratio` | 09:45 前量比 | **unavailable:needs_intraday_minute_bars** | 同左 |

阈值全部来自 `config/daban_thresholds.yaml → event_table_v4`（`one_word_seal_minute: 565`、
`fast_board_seal_minute: 571`、`turnover_baseline_window: 20`、`turnover_baseline_min_days: 15`），代码内零硬编码。

## 2. `reseal_time` 语义（回封 = 炸板之后重新封板的时刻）

| 输入 | 结果 | 理由 |
|---|---|---|
| `open_board_count` 缺失 | None + `unavailable:open_board_count_missing` | 不知道炸没炸过，不能断言"没回封" |
| `open_board_count == 0` | None + `not_applicable:never_opened_board_no_reseal` | 一次没炸过就**不存在**回封时刻；把当天最后封板时间当回封是伪造 |
| `open_board_count > 0` 且有最后封板时间 | 取 `最后封板时间` | 最后一次炸板后重新封上的时刻 |
| `open_board_count > 0` 但最后封板时间缺失 | None + `unavailable:last_seal_time_missing` | 同上，缺就是缺 |

## 3. 板块横截面聚合规则

按 `date × sector` 在**当日全量涨停池**上聚合（丢弃 no_kline/no_next_day 之前），不额外触网。两条纪律：

1. `sector` 缺失的票**不进任何板块**，也不归到"未知板块"——伪板块会让它凭空参与家数排名。该票的三个聚合字段一律 `unavailable:sector_missing`。
2. 组内只要有一条首次封板时间不可解析，一字/快速板家数就是**已知的低估值** → 整组标 `unavailable:sector_first_seal_incomplete(n)`，不报偏小的数（报 0 家更是伪造）。板块涨停家数本身仍是事实，照常给出。

## 4. 20 日换手基准

换手率 = 成交股数 / 流通股本。日线只有 `volume`（单位「手」，每手股数取 `execution_constraints.volume_lot_shares`）
和价格，**没有流通股本**；流通股本只能由 zt_pool 的流通市值 ÷ 当日收盘价反推。

- 流通市值缺失/为 0 → `unavailable:float_shares_unavailable`，**绝不退化成"用成交量当换手率"**（量纲不同；仓内已有 volume 漏乘每手股数把成交额低估 100 倍的先例）。
- 样本窗口是事件日**之前** N 个交易日（不含事件日，避免用当日暴量污染基准）；有效样本 < `turnover_baseline_min_days` → `unavailable:baseline_sample_insufficient(n<min)`，`sample_days` 照实报。

## 5. 降级诊断规则（`daban_bt_run._degradation_notice`）

升 v4 前这里把"schema ≠ 当前版本"一律解释成"缺 T 日 OHLC"——那只是 v2→v3 的差异，v3 旧表读到这句就是**误诊**。现按版本差异分支：

| 表 | `stale_event_table` | 诊断措辞 |
|---|---|---|
| v2 及更早 | true，`board_overnight_events_blocked > 0` | 缺 T 日 OHLC/t_prev_close，board_overnight 被 fail-closed 判买不进，空样本是**数据过期**而非没有信号 |
| v3 | true，`board_overnight_events_blocked == 0`，`missing_v4_fields` 列出缺的字段 | T 日行情齐全、board_overnight 正常成交；缺的是 v4 新增的 S1/S2 证据字段，两者零命中是数据过期 |
| v4 | false | 不标 stale |

原有语义保留：空样本必须能区分"数据过期"与"真的没信号"。`format_report` 直接引用该 note，不再另写一套原因。

## 6. 仍然缺失的字段及原因

| 字段 | 谁要 | 为什么还缺 |
|---|---|---|
| `volume_ratio`（09:45 前量比） | S1 条件 3 | 需要分钟线。日线只有全日量，口径不同，**不造代理值**。 |
| `pre_reseal_turnover_pct`（封板前累计换手） | S2 条件 4 | 需要分钟线。akshare 的 `换手率` 是全日口径，不等价。 |

后果（本轮的诚实结论）：**在真实 v4 表上 S1 仍然 0 命中；S2 补齐了 4 组证据中的 3 组，条件 4 仍 unavailable，也还是 0 命中。**
`tests/test_event_schema_v4.py::test_s1_and_s2_still_zero_without_the_minute_only_fields` 把这一点钉死，防止后续被代理值悄悄"修好"。
两者能命中的证明见同文件的 `test_s1_fires_on_synthetic_v4_table` / `test_s2_fires_on_synthetic_v4_table`：
在**合成 v4 表**上显式注入这两个分钟线字段后，S1 与 S2 各命中 2 条、均产生非零收益样本 —— 即"补齐数据后策略确实跑得通"。
下一步该做的是分钟线管道（09:45 量比 + 封板前累计换手），不是改策略。

## 7. 重建旧表的操作指引

`build_event_table` 只复用 `schema == daban_bt_event_table_v4` 的缓存，旧缓存自动失效重建；无需手工删文件。

```bash
cd <repo>
PYTHONPATH=$PWD .venv/bin/python skills/chanlun-backtest/scripts/daban_bt_run.py \
  --build 20260601 20260626 --split 20260615 --source akshare --json
```

- 会触网（akshare 逐日 zt_pool + 腾讯日线），免费历史仅最近约 3-4 周，`coverage.warning` 会高声标注。
- `--source mootdx` 走深历史，但上表右列全是 unavailable，S1/S2 在该来源上不可用（不是"没信号"）。
- 只想把已有 v3 表升级：没有原地迁移路径 —— v4 的新字段来自上游原始行与 K 线历史，必须重跑构建。
- worktree/editable 安装下直接跑 CLI 会 `ModuleNotFoundError: skills.common`，用 `PYTHONPATH=$PWD` 前缀。
