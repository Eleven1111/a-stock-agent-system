# AUTOPILOT — 后台常驻任务登记

本仓库运行的所有后台/定时任务在此登记。新增任务必须同步登记，否则无法追溯"谁在跑、怎么停"。

## com.a-stock-cc.scheduler（launchd 用户代理）

> **状态：2026-08-07 19:11 已停止并 disable**（用户要求）。生产在部署机一侧，本机这个实例
> 只产出候选/研究流水，`portfolio.json` 为空账户。停止方式见下方"停止"，已额外执行
> `launchctl disable` 防止下次登录自动加载；plist 与 `~/.a-stock-agent-cc` 数据均未删除。
> 重启需要先 `launchctl enable gui/$(id -u)/com.a-stock-cc.scheduler` 再 load。

| 项 | 值 |
|---|---|
| 名称 | `com.a-stock-cc.scheduler` |
| 配置 | `~/Library/LaunchAgents/com.a-stock-cc.scheduler.plist` |
| 心跳 | 每 60 秒（`StartInterval`）唤醒一次 |
| 执行 | `cd <本仓库> && PYTHONPATH=skills/common .venv/bin/python scripts/cron_dispatch.py` |
| 作业来源 | `cron/hermes-cron-manifest.json`（72 个作业，当前 59 个 enabled） |
| 状态根 | `A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc` |
| 运行模式 | `A_STOCK_RUNTIME=hermes` |
| 调度器日志 | `$A_STOCK_STATE_HOME/cron/scheduler.{out,err}.log` |
| 作业输出日志 | `$A_STOCK_STATE_HOME/cron/dispatch-jobs.log` |
| 去重状态 | `$A_STOCK_STATE_HOME/cron/dispatch_state.json` |
| 执行事件流 | `$A_STOCK_STATE_HOME/cron/execution_trace.jsonl`（只追加，shadow 观测） |

**停止：**

```bash
launchctl unload ~/Library/LaunchAgents/com.a-stock-cc.scheduler.plist
launchctl disable gui/$(id -u)/com.a-stock-cc.scheduler
```

**确认已停：** `launchctl list | grep a-stock` 无输出；再等两个心跳（>120 秒）复查
`~/.a-stock-agent-cc/cron/output/job_runs.json` 时间戳不再前进。注意 plist 带
`AbandonProcessGroup=true`，已 detach 的子任务会跑完，不会随 unload 被杀。

**重新启动：**

```bash
launchctl enable gui/$(id -u)/com.a-stock-cc.scheduler
launchctl load ~/Library/LaunchAgents/com.a-stock-cc.scheduler.plist
```

### 诊断与回滚

```bash
# 一次运行的完整重建 + 全 manifest 覆盖检查
python scripts/execution_trace_report.py --coverage
```

关注三项：`trace_gaps` 必须为空；`shadow_gate` 三项必须全 true；`duration_seconds.p95`
相对接入 trace 前的基线不得回归超过 5%。连续五个交易日满足这三项，才算通过 Shadow 门。

回滚开关（作业行为不受影响，只停止写 trace）：

```bash
# 在 launchd plist 的 EnvironmentVariables 中加入，然后 unload / load
A_STOCK_EXECUTION_TRACE=off
```

**不要**为了回滚删除已经写入的 `execution_trace.jsonl` 或 `signal_ledger.jsonl`。

类型化命令：dispatcher 只接受 `command_argv` 数组并以 `shell=False` 执行。作业启动失败时
先看 `dispatch-jobs.log` 与调度器 err 日志里的 `skip job ...` 行 —— 可执行文件缺失、cwd
越界、argv 里出现 shell 元字符都会 fail closed 并记录原因，不会静默降级回 shell。

### 集合竞价链路失败汇总（auction-chain-watch）

| 项 | 值 |
|---|---|
| id | `auction-chain-watch`（`enabled: true`） |
| 调度 | `35 9,10 * * 1-5`（09:35 竞价链应已收口，10:35 复查做双保险） |
| 执行 | `python scripts/cron_failure_watch.py`（只读 artifact，不写业务状态） |
| 推送 | `deliver: feishu_direct`（2026-08-20 起两个故障告警作业放行回飞书，见下）；全绿时无输出，`silent_when_no_signal` 直接静默 |
| 停止 | manifest 里把该作业 `enabled` 改为 `false`（dispatcher 下一次心跳即生效） |

存在理由：本机调度器 `Popen` fire-and-forget 起作业，**没有任何消费者读退出码**，
所以链路失败在本机原本完全不可见（issue #159）。它读当日竞价链 5 个作业最新
artifact 的 `status`，并把 `missing`（当日无 artifact，多半是 Mac 睡眠错过 launchd
心跳）与 `failed/timeout/blocked`（跑了但没跑通）分开报 —— 两者运维动作不同。

手动跑：

```bash
A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc PYTHONPATH=skills/common \
  .venv/bin/python scripts/cron_failure_watch.py --json
```

### 飞书出口：默认全部关闭（2026-08-29）

所有 cron 作业均为 `local`/`silent`/`origin`，manifest 中不得出现
`feishu_direct`。聊天消息与 Serenity 文档共用 fail-closed 总开关：仅同时显式配置
`A_STOCK_FEISHU_EGRESS_ENABLED=true` 及相应目标时才允许调用 `lark-cli`。
只配置 `A_STOCK_FEISHU_CHAT_ID` 不会恢复推送；重大新闻旁路也受同一总开关约束。

### 开盘前体检（preopen-preflight）

把仓库里早就有、却一个都没排期的体检脚本聚合成一个开盘前入口。
issue #239 的验收标准之一：开盘前能自动发现注册漂移、推送未配置、
模型认证/余额异常和网关端口冲突。

| 项 | 值 |
|---|---|
| id | `preopen-preflight`（`enabled: true`） |
| 调度 | `5 8 * * 1-5`（早于 08:20 的 `hot-money-context-backfill`） |
| 执行 | `python scripts/preopen_preflight.py --json`（只读，不写业务状态） |
| 推送 | `deliver: feishu_direct` + `silent_when_no_signal`：**全绿静默**，有红/黄才推 |
| 超时 | 60s（`short` 档；本机实测含 easy_tdx 建连探测约 1.2s） |
| 停止 | manifest 里把该作业 `enabled` 改为 `false`（dispatcher 下一次心跳即生效） |

六个检查项：

| 项 | 查什么 | 红的条件 |
|---|---|---|
| `config` | 所有注册配置文件是否仍合法 | 校验未通过 |
| `state` | 状态根身份、关键 JSON 可读 | 身份异常或 JSON 损坏（split brain 只报黄） |
| `registration` | manifest ↔ OpenClaw 注册漂移 | 两边不一致（**读不到注册表报黄，不算绿**） |
| `delivery` | 有 `feishu_direct` 作业时 chat id 是否已配 | 声明了推送但送不出去 |
| `auction_sources` | easy_tdx / mootdx 可导入 + easy_tdx 建连 | easy_tdx 未安装（连不上只报黄，开盘可能恢复且链路有兜底） |
| `gateway` | 网关日志里的 401 / 402 / EADDRINUSE | 出现任一类 |

**返回码恒为 0**：体检发现问题不等于本次运行失败。返回非 0 会让 DAG 把它当失败
依赖、反而挡住后面的链 —— 一个用来防止链路停摆的作业自己变成停摆的原因，
是最糟糕的形态。红项通过 artifact 与推送送出去，不通过退出码。

**模型认证/余额与端口冲突为什么只读日志**：本仓库不直接调用任何模型厂商 API
（模型回合发生在 OpenClaw 网关侧），401/402/EADDRINUSE 只在网关日志里出现。
凭空造一个厂商客户端来"探活"等于发明一个不存在的依赖。日志默认在 `/tmp/openclaw`，
**重启即清空**，所以读不到就报黄。

手动跑：

```bash
A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc \
  .venv/bin/python scripts/preopen_preflight.py --json           # 完整体检
.venv/bin/python scripts/preopen_preflight.py --json --no-probe  # 跳过 easy_tdx 建连
```

### 每日运行诊断包归档（daily-diagnostics）

| 项 | 值 |
|---|---|
| id | `daily-diagnostics`（`enabled: true`） |
| 调度 | `10 23 * * *`，`trading_day_policy: calendar_day`（**非交易日也跑**） |
| 执行 | `python scripts/daily_diagnostics.py --archive`（纯只读聚合） |
| 产出 | `$A_STOCK_STATE_HOME/diagnostics/<日期>.md` + 同名 `.json`，**滚动保留 30 天** |
| 推送 | `deliver: origin`；回给调度器的只有一行摘要（报告本体在磁盘上） |
| 停止 | manifest 里把该作业 `enabled` 改为 `false`（dispatcher 下一次心跳即生效） |

存在理由：系统跑在两个互相看不见的 Agent 里（OpenClaw 网关 + Hermes 调度器），
排障证据散落五处。**事故当天再去翻往往已经没了** —— OpenClaw 主日志默认在
`/tmp/openclaw/`，macOS 重启即清空。每天先存一份，出事时直接把这个文件发出去。

**非交易日也跑是刻意的**：调度器整体停摆这种故障，恰恰只能从「非交易日也没有
报告」看出来；只在交易日跑的话，周末停摆到周一才会暴露。

**Markdown 给人读，`.json` 给聚合读。** 单日报告永远回答不了「问题有没有真的
收敛」，所以归档时同时落一份结构化诊断，摘要行里带近 5 日的
新增 / 重复 / 已修 / 待验证四个数：

```bash
python scripts/daily_diagnostics.py --json                    # 当日结构化诊断
python scripts/daily_diagnostics.py --rollup --days 5         # 跨天聚合
```

**「待验证」是刻意留的一栏**：一个问题不再出现，可能是修好了，也可能是那个作业
当天压根没跑。只有主体在最后一天**确实被观测到**才算 `resolved`，否则记
`unverified` —— 用空集证明「已修复」是最容易骗过自己的一种假绿。

报告已按规则脱敏（`sk-`/`ghp_`/`xox*`/Bearer/`*key|token|secret|chat_id=`/长十六进制），
但**传出前请自行再扫一眼**。清理只认 `YYYY-MM-DD.md` 命名，手工另存的事故存档
（如 `2026-08-06-incident.md`）不会被删。

手动跑：

```bash
python scripts/daily_diagnostics.py --out ~/Desktop/diag.md          # 临时看一份
python scripts/daily_diagnostics.py --date 2026-08-06 --archive      # 补归档某一天
```

### 待启用：公告召回雷达（announcement-radar）

三个作业已登记进 manifest，但**当前 `enabled: false`**，处于待观察状态：

| id | schedule | 说明 |
|---|---|---|
| `announcement-radar-evening` | `30 21 * * 1-5` | 主跑，抓当日全市场公告 |
| `announcement-radar-premarket` | `40 7 * * 1-5` | 补抓凌晨增量，赶在 08:30 候选池引导前 |
| `announcement-radar-weekend` | `0 9 * * 6` | 周六跑（实测 08-01 周六仍有 1440 条） |

启用前需连续 5 个交易日手动跑并满足：抓取量 > 1000、分类命中率 ≥ 85%、
召回 100~250 条、`cninfo` 源既有调用失败率无上升。手动跑：

```bash
A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc PYTHONPATH=skills/common \
  .venv/bin/python skills/announcement-radar/scripts/radar.py --date today --emit-brief
```

**耗时 6~14 分钟**（2026-08-04 两次实测：5:59 与约 13 分钟，网络波动导致差异大）：
全市场约 50 次接口请求 × `http_client` 2.5s/源 节流。
因此 `run.timeout_seconds` 设为 1800，不可套用其他作业的 15~60s。
产物：`$A_STOCK_STATE_HOME/cron/output/announcement-radar/<date>.json`。

### 研究数据集物化（research-dataset-build）

把 catalog 声明的数据集物化成契约行，让研究数据真正开始积累。此前 catalog
只有契约、没有生产方，没有任何数据集在增长。

| 项 | 值 |
|---|---|
| 作业 id | `research-dataset-build` |
| 调度 | `40 16 * * 1-5`（排在结算作业 `performance-daily` 16:10 之后） |
| 执行 | `python scripts/build_research_datasets.py --json` |
| 依赖 | `performance-daily`（同交易日，最大延迟 120 分钟） |
| 交付 | `local`（只写本地产物，不推送） |
| 产物 | `$A_STOCK_STATE_HOME/skills/stock-triage/data/dataset_<dataset_id>_<asof>.json` |
| 超时 | 120s（`standard` 档；取行情的方向数据集走并发，4 worker） |

**停止：** 把 manifest 中该作业的 `enabled` 改为 `false`，或停整个调度器（见上）。

```bash
python -c "import json;p='cron/hermes-cron-manifest.json';d=json.load(open(p));[j.update(enabled=False) for j in d['jobs'] if j['id']=='research-dataset-build'];json.dump(d,open(p,'w'),ensure_ascii=False,indent=2)"
```

**手动跑（离线，只建已结算信号数据集，不触网）：**

```bash
A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc PYTHONPATH=skills/common \
  .venv/bin/python scripts/build_research_datasets.py --settled-only --json
```

注意：`coverage_ratio` 衡量的是**投影完整性**（多少源记录成功变成行），
不是样本是否足够。3 条结算样本同样会得到 1.0。样本量守卫在下游
（`cross_sectional_direction` 的 `min_pairs_per_cohort = 100`）。

### 收盘历史日K缓存更新（market-history-cache）

给竞价链和回测提供本地日线底仓：akshare 免费源只回溯最近 3~4 周，前收/前一日量额
缺失会让竞价量比算不出来。本作业收盘后按缺失日增量补齐，落成一张本地 SQLite。

| 项 | 值 |
|---|---|
| id | `market-history-cache`（`enabled: true`） |
| 调度 | `10 15 * * 1-5`（收盘后 10 分钟） |
| 执行 | `python scripts/market_history_cache.py --json` |
| 数据源 | BaoStock（全市场个股 + `sh.000300` 指数基准）；**未安装时输出 `status: blocked`**，不报错、不影响其他作业 |
| 产物 | `$A_STOCK_STATE_HOME/market/history.sqlite3` |
| 交付 | `local`（只写本地产物，不推送） |
| 超时 | 300s（`standard` 档；全市场按缺失日增量，单票失败只记 `failed` 不中断批次） |
| 停止 | manifest 里把该作业 `enabled` 改为 `false`（dispatcher 下一次心跳即生效） |

**手动跑（先空跑看范围，再实写）：**

```bash
A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc \
  .venv/bin/python scripts/market_history_cache.py --dry-run --json
```

### 六策略统一证据数据集（strategy-evidence-daily）

六个策略共用一条深证据管道，不再各自拼候选池、竞价、日线和分钟 sidecar。Evidence
Cohort 是「官方涨停事件 + 竞价短名单 + 60 天内仍在跟踪的市场最高板」的并集；每天
收盘后只对并集内每只股票请求一次腾讯全天分时，官方涨停池也只请求一次。

| 项 | 值 |
|---|---|
| id | `strategy-evidence-daily`（`enabled: true`） |
| 调度 | `20 23 * * 1-5`（等待 23:15 `cycle-state-shadow` 固化当日情绪序列） |
| 执行 | `python scripts/strategy_evidence_daily.py --json` |
| cohort | 官方涨停池 + 竞价短名单 + 在跟踪龙头；上限 160，只去重、不截断 |
| 外部请求 | 官方涨停池 1 次；cohort 每只腾讯分时 1 次，串行共享节流 |
| 产物 | `$A_STOCK_STATE_HOME/skills/stock-triage/data/strategy_evidence/{asof}.json` |
| 超时 | 1200s（`long` 档，覆盖双域名失败时的最坏退避） |
| 停止 | manifest 里把该作业 `enabled` 改为 `false` |

超过 160 只时整批失败并暴露 `EvidenceBudgetExceeded`，绝不静默截断。正式产物只接受
官方收盘事件、腾讯当日分时和本地日线；BaoStock 5 分钟重建仍只属于
Exploratory Reconstruction，不得写成 Canonical Forward Evidence。每个策略的缺字段、
ready 记录数和覆盖率都固化在 `coverage`，缺数据是 `unavailable`，不是负样本。

S6 的 `S_t` 需要 180 个交易日预热，不能等上线后再空转九个月。22:50 的
`sentiment-daily-backfill` 只读 `history.sqlite3`，在已有前向记录**之前**一次性播种
180 日；达到 180 日后每次直接 `skipped`。它不会用日线近似覆盖已经固化的前向记录，
日线拿不到的字段继续为 `unavailable`。23:15 的 `cycle-state-shadow` 把它列为可选依赖，
因此周末的 calendar-day 诊断不会被一个仅交易日运行的 bootstrap 挡住。

### 六策略每日影子评估（strategy-shadow-daily）

六个游资研究策略（rank_surprise / divergence_reseal / assist_arbitrage /
preleader_arbitrage / reverse_volume / ice_point_reversal）在实盘外单独跑一遍，
只为积累样本。**SHADOW ONLY**：不调用 `strategy_registry`、不改排序与仓位、
不写 `portfolio.json`、不产生 `signal.opened`。

| 项 | 值 |
|---|---|
| id | `strategy-shadow-daily`（`enabled: true`） |
| 调度 | `40 23 * * 1-5`（等待证据数据集、S4 盘前表与当日情绪序列） |
| 执行 | `python scripts/strategy_shadow_runner.py --json` |
| 输入 | 当日不可变 `strategy_evidence_daily_v1`；不再现场拼 sidecar |
| 产物 | `$A_STOCK_STATE_HOME/skills/stock-triage/data/strategy_shadow/{asof}.json` |
| 交付 | `local`（只写本地产物，不推送） |
| 超时 | 120s（`standard` 档） |
| 停止 | manifest 里把该作业 `enabled` 改为 `false`（dispatcher 下一次心跳即生效） |

**fail-closed 口径**：输入 asof 与请求日不符、非 canonical forward 或混入 exploratory
reconstruction 都直接报错；同日已有产物但输入 hash 不同
时拒绝覆盖（不可变产物）；任一策略缺证据记 `unavailable`，不退化成 `no_signal`。

`preleader_arbitrage` 消费**前一日**的盘前表（见下条作业）。找不到 D-1 的表时报
`unavailable` 并带原因，不会退化成 `no_signal` —— 缺表是缺证据，不是「不在表内」。

**一处口径判断（不是字段搬运）**：S4 还需要 D0 的龙头确认，runner 把候选池的
`first_seal`（首次封板时刻）映射成 `confirmed` / `confirmed_time` /
`evaluation_time`，即把「当日封上板」等同于「该标的已确认」。没封板的行不给
`confirmed`，龙头保持不可判定。这条等价关系取自原案例形态，**未经样本外验证**；
读 S4 结果前先确认你接受它。

### 四维权重影子研究（four-dim-scorer / four-dim-weight-shadow）

两条作业只积累和评估四维评分研究样本，不改变生产权重。`four-dim-scorer` 按
trend / daban 配额读取当日不可变候选池和本地缓存；`four-dim-weight-shadow` 在每条
lane 满 60 个有效交易日时冻结模型，再前向积累最早 60 个未见交易日作为 OOS。

| 项 | 值 |
|---|---|
| ids | `four-dim-scorer`、`four-dim-weight-shadow`（均 `enabled: true`） |
| 调度 | `15:18` 评分；`23:30` 标签与权重影子评估 |
| 外部请求 | 无；只读 dated candidate pool、`history.sqlite3`、催化与 Serenity 缓存 |
| 产物 | append-only observation v2、冻结模型、标签、shadow 报告与回滚说明 |
| 生产影响 | `live_effect=none`；禁止自动修改 `config/scoring.yaml` |
| 停止 | manifest 里将两条作业的 `enabled` 改为 `false` |

冻结产物的 `fit_cutoff`、训练集哈希、模型哈希和权重一旦生成，后续运行只能追加
cutoff 之后的 OOS；不得滚动重拟合，也不得把第 61 个以后日期替换进首个 60 日 OOS。

### S4 盘前表构建（preleader-pretable-build）

D-1 晚间建「龙头候选 → 属性 → 同属性候选」映射表，供次日 S4 使用。策略的成败点
是这张表必须是真盘前产物：盘中现算等于用当日信息选样本。

| 项 | 值 |
|---|---|
| id | `preleader-pretable-build`（`enabled: true`） |
| 调度 | `40 16 * * 1-5`（在 15:10 `market-history-cache` 之后，用它的日线缓存） |
| 执行 | `python scripts/preleader_pretable_build.py --json` |
| 输入 | 当日 `candidate_pool_latest.json` + 本地日线缓存（20日均额）+ 巨潮公告 |
| 产物 | `$A_STOCK_STATE_HOME/skills/stock-triage/data/preleader_pretable/{as_of}.json` |
| 交付 | `local`（只写本地产物，不推送） |
| 超时 | 1500s（`long` 档；公告逐只网络取数，306 只实测 >10 分钟） |
| 停止 | manifest 里把该作业 `enabled` 改为 `false`（dispatcher 下一次心跳即生效） |

**证据缺失一律显式退化，不出表**（`status: degraded` + 命名缺口）：

| 缺口 | 为什么不能照常出表 |
|---|---|
| `avg_turnover_20d_source_unavailable` | 日线缓存整体不可用时照常建表，每只成分股都会被记成「流动性不足」——一张「所有人都不合格」的表在下游和真表长得一模一样 |
| `member_pool_exceeds_announcement_scan_budget` | 静默截断会让被截掉的票以「没有利空」的身份留在表里 |

单只公告取数失败的票**不进成分股池**，记入 `announcement_scan_failed`；把取数失败
当成「没有利空」是把未知洗成干净。只有 `status == "ok"` 的表才会被次日消费。

### 历史与注意事项

- 2026-07-20 之前，该 launchd 代理指向的是另一个仓库 `a-stock-agent-claude-code`。本仓库虽然维护着完整 manifest，但缺 `scripts/cron_dispatch.py`，因此本仓库的改动（含 PR #114 / #117）从未在生产生效。当日已将 `cd` 路径切至本仓库并补上 dispatcher（PR #119）。
- `A_STOCK_RUNTIME` 必须为 `hermes`。切换当日曾沿用旧值 `claude-code`，导致 `run_agent_dag.py` 直接失败——该值是另一个仓库的运行模式。
- dispatcher 从工作区直接运行，**不经过构建**。因此工作区里未提交的改动会立即进入生产，务必保持工作区与 main 一致。
