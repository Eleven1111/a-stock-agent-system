# A-stock Agent System 每日定时任务审计报告

审计日期：2026-07-07  
审计范围：`cron/hermes-cron-manifest.json` 中 48 个 jobs、`scripts/run_agent_dag.py`/`scripts/hermes_job_runner.py` 的 DAG 依赖执行逻辑、`cron/output/job_runs.json` 最近运行记录、`../HEARTBEAT.md` 的禁用/降频记录。  
结论口径：本报告只给清理建议，不修改任何文件。

## 总体结论

- manifest 当前有 48 个任务，其中 41 个 `enabled=true`，7 个 `enabled=false`。
- `scripts/generate_openclaw_cron.py` 只安装 `enabled=true` 的任务；但 `run_agent_dag.py` 在执行某个 enabled 目标时，会按 `context_from` 递归运行依赖，**不会因为依赖 job 的 `enabled=false` 而跳过**。
- 因此当前存在 3 个“enabled 任务依赖 disabled 任务”的隐性问题：
  - `candidate-discovery` 必需依赖 `hk-a-linkage`，但 `hk-a-linkage` 已禁用。
  - `portfolio-check` 必需依赖 `four-dim-scorer`，但 `four-dim-scorer` 已禁用。
  - `closing-triage` 必需依赖 `four-dim-scorer`，但 `four-dim-scorer` 已禁用。
- `../HEARTBEAT.md` 与 manifest 不一致：
  - HEARTBEAT 写明已禁用：`social-attention-preopen/midday/close`、`serenity-refresh-plan`、`intraday-alert`。
  - manifest 中这些任务仍然 `enabled=true`。
- `cron/output/job_runs.json` 只有 9 个 job 的 138 条记录，且全部为 `blocked_state`，最近一次集中在 2026-07-06；这说明当前运行账本主要反映状态身份校验失败，不能证明业务脚本本身成功或失败，但能说明这些高频任务近期没有真正产出有效运行结果。

## DAG 与投递规则观察

- `deliver=local` 或 `silent` 的任务不会在 `--emit-target` 下向用户回消息，只写 artifact/缓存。
- `deliver=feishu_direct` 直接推送飞书，并受 `max_output_chars` 截断。
- `deliver=origin` 通过当前来源通道输出；若配置了 `silent_when_no_signal` 且无信号，会静默。
- `context_from` 是硬依赖，除非写入 `dependency_policy.optional_jobs`；硬依赖失败会阻断下游。
- 禁用 job 不等于不能被 DAG 依赖运行；禁用只影响 cron 安装生成。

## HEARTBEAT 对照

HEARTBEAT 已记录降频：

| 任务 | HEARTBEAT 记录 | manifest 当前 |
| --- | --- | --- |
| `news-monitor-intraday` | 每 5 分钟降为每 15 分钟 | `2,17,32,47 9-11,13-14 * * 1-5`，符合 |
| `catalyst-trigger` | 每 15 分钟降为每 30 分钟 | `3,33 9-11,13-14 * * 1-5`，符合 |
| `news-monitor` | 7 次/天降为 5 次/天 | `0 9,11,15,20,22 * * 1-5`，符合 |

HEARTBEAT 与 manifest 冲突：

| 任务 | HEARTBEAT | manifest |
| --- | --- | --- |
| `social-attention-preopen` | 已禁用 | enabled=true |
| `social-attention-midday` | 已禁用 | enabled=true |
| `social-attention-close` | 已禁用 | enabled=true |
| `serenity-refresh-plan` | 已禁用 | enabled=true |
| `intraday-alert` | 2026-06-26 主人明确不需要 | enabled=true |

## 最近运行成功情况

`cron/output/job_runs.json` 中出现过的 job 全部是 `blocked_state`，无 `ok`：

| 任务 | 记录数 | 最近状态 | 最近时间 |
| --- | ---: | --- | --- |
| `official-policy-watch` | 54 | blocked_state | 2026-07-06 14:33 |
| `intraday-alert` | 32 | blocked_state | 2026-07-06 14:30 |
| `news-monitor-intraday` | 28 | blocked_state | 2026-07-06 14:32 |
| `catalyst-trigger` | 14 | blocked_state | 2026-07-06 14:33 |
| `hk-a-linkage` | 2 | blocked_state | 2026-07-06 13:45 |
| `hot-money-afternoon-checkpoint` | 2 | blocked_state | 2026-07-06 13:15 |
| `market-pulse-1314` | 2 | blocked_state | 2026-07-06 13:14 |
| `news-monitor` | 2 | blocked_state | 2026-07-06 11:00 |
| `social-attention-midday` | 2 | blocked_state | 2026-07-06 11:37 |

未出现在账本中的任务不能据此判断成功或失败，只能说明最近账本样本没有覆盖。

## 逐任务评估

| 任务 | enabled | 当前频率 | 推送方式 | max | 下游依赖 | 价值 | 必要性评级 | 理由 |
| --- | --- | --- | --- | ---: | --- | --- | --- | --- |
| `morning-note` | true | 工作日 07:00 | feishu_direct | 4500 | 无 | 高 | 🟢保留 | 晨会直推，整合隔夜、新闻、事件和日历，是盘前人工阅读入口。 |
| `provider-health` | false | 工作日 08:05 | local | 3000 | 无 | 低 | 🔴删除 | 已禁用且无下游、无推送；更适合保留为手工诊断脚本，不需要 cron。 |
| `global-preopen` | true | 工作日 08:15 | feishu_direct | 30000 | `global-evening`,`hk-a-linkage`,`news-monitor` | 高 | 🟢保留 | 盘前全球市场直推并被资讯监控/港A链路引用，影响宏观风险判断。 |
| `global-evening` | true | 工作日 22:30 | local | 3000 | 无 | 低 | 🟡降频 | 晚间归档无推送、无下游；可改为每周或仅在海外大波动时触发。 |
| `company-event-opportunity-scan` | true | 工作日 08:35 | local | 3000 | 无 | 中 | 🟡降频 | 公司事件有用但无下游和无推送；建议并入 morning-note/preopen brief 或降为每周加事件触发。 |
| `social-attention-preopen` | true | 工作日 08:42 | local | 1800 | `behavioral-finance-preopen`,`candidate-preopen` | 低 | 🔴删除 | HEARTBEAT 已列为禁用，且对候选池是 optional 弱证据，删除不应阻断主链。 |
| `behavioral-finance-preopen` | true | 工作日 08:43 | local | 3000 | 无 | 低 | 🔴删除 | 只做解释性归档、无下游、无推送，边际收益低。 |
| `candidate-preopen` | true | 工作日 08:45 | local | 2500 | `auction-snapshot`,`auction-market-snapshot` | 高 | 🟢保留 | 竞价采集的前置候选池引导，删除会影响集合竞价链路。 |
| `preopen-intelligence-brief` | true | 工作日 08:50 | origin | 2400 | 无 | 高 | 🟢保留 | 盘前情报直接推送，是用户开盘前决策阅读项。 |
| `social-attention-midday` | true | 工作日 11:37 | local | 1800 | 无 | 低 | 🔴删除 | HEARTBEAT 已列为禁用；无下游、无推送，午间独立快照价值不足。 |
| `social-attention-close` | true | 工作日 15:04 | local | 1800 | `candidate-discovery`,`behavioral-finance-close` | 低 | 🟡降频 | HEARTBEAT 已列为禁用，但收盘候选发现仍 optional 引用；建议改为非每日或仅保留收盘版本。 |
| `hot-money-context` | true | 工作日 15:02 | local | 1200 | `candidate-discovery`,`behavioral-finance-close` | 高 | 🟢保留 | 涨停梯队和板块热度进入候选发现，是次日温度闸门基础。 |
| `hot-money-context-backfill` | true | 工作日 08:40 | local | 1200 | 无 | 中 | 🟢保留 | 自愈上一交易日梯队缺档，避免次日基准日期错滚。 |
| `candidate-discovery` | true | 工作日 15:07 | local | 2500 | `four-dim-scorer`,`theme-strength-daily`,`research-dispatch`,`candidate-fsm-sweep`,`catalyst-trigger` | 高 | 🟢保留 | 全市场动态候选池是盘后到次日盘中的核心输入；但需修正 `hk-a-linkage` 禁用硬依赖。 |
| `behavioral-finance-close` | true | 工作日 15:12 | local | 3000 | 无 | 低 | 🔴删除 | 收盘行为金融只归档且无下游，建议从每日 cron 移除。 |
| `auction-snapshot` | true | 工作日 09:15-09:23 每分钟 | local | 800 | `auction-finalize` | 高 | 🟢保留 | 集合竞价分钟快照直接支撑 09:26 收口和开盘计划。 |
| `auction-market-snapshot` | true | 工作日 09:24 | local | 800 | `auction-finalize` | 高 | 🟢保留 | 全市场竞价异动快照用于研究情报和短名单对照。 |
| `auction-finalize` | true | 工作日 09:26 | local | 4500 | `open-confirmation` | 高 | 🟢保留 | 输出竞价因子、计划和公告质检，是开盘确认硬前置。 |
| `auction-intelligence-brief` | true | 工作日 09:27 | origin | 2000 | 无 | 高 | 🟢保留 | 直接推送集合竞价结果，用户盘前最后决策窗口会看。 |
| `open-confirmation` | true | 工作日 09:35 | local | 5000 | `hot-money-morning-checkpoint`,`hot-money-afternoon-checkpoint`,`intraday-alert`,`capital-flow` | 高 | 🟢保留 | 生成买入区间、追价线、止损、T+1约束，是交易辅助主链核心。 |
| `open-intelligence-brief` | true | 工作日 09:36 | origin | 2400 | 无 | 高 | 🟢保留 | 直接推送门禁后信号和过滤原因，补足用户可读解释。 |
| `hot-money-morning-checkpoint` | true | 工作日 09:50 | origin | 2200 | 无 | 中 | 🟢保留 | 对主线龙头承接做一次确认，频率低且贴近开盘决策。 |
| `hot-money-afternoon-checkpoint` | true | 工作日 13:15 | origin | 2200 | 无 | 中 | 🟡降频 | 午后回流有参考价值，但近期账本 blocked；可改为仅在上午候选有强信号时触发。 |
| `intraday-alert` | true | 工作日 09-11、13-14 每 15 分钟 | origin | 2500 | `capital-flow` | 中 | 🟡降频 | HEARTBEAT 记录主人明确不需要；若保留，应从 `capital-flow` 硬依赖移除并改为持仓/订阅触发。 |
| `capital-flow` | true | 工作日 10:30、14:30 | feishu_direct | 3500 | `candidate-discovery`,`four-dim-scorer`,`closing-triage` | 高 | 🟢保留 | 资金流直推且进入收盘候选和复盘，是核心交易证据。 |
| `hk-a-linkage` | false | 工作日 09:45、13:45、14:45 | local | 2500 | `candidate-discovery`,`four-dim-scorer` | 低 | 🟡降频 | 已禁用但仍被 `candidate-discovery` 硬依赖；建议改 optional 或降为盘前/收盘一次。 |
| `four-dim-scorer` | false | 工作日 15:18 | origin | 4000 | `portfolio-check`,`closing-triage` | 高 | 🟢保留 | 虽禁用但被风控和收盘 triage 硬依赖；要么恢复启用，要么重构下游依赖。 |
| `portfolio-check` | true | 工作日 15:25 | origin | 3000 | `closing-triage` | 高 | 🟢保留 | 持仓风控检查是方向建议的必要防线；当前需解决 `four-dim-scorer` 前置问题。 |
| `closing-triage` | true | 工作日 15:35 | origin | 3500 | `stock-intelligence-refresh`,`serenity-refresh-plan`,`theme-strength-daily`,`research-dispatch`,`performance-daily` | 高 | 🟢保留 | 盘后复盘、Kanban 派发和后续研究/绩效的核心汇总。 |
| `stock-intelligence-refresh` | false | 工作日 15:40 | local | 2500 | `serenity-refresh-plan` | 中 | 🟡降频 | 候选筹码/机构证据有用但成本高且只归档；建议按候选变化或每周刷新。 |
| `serenity-refresh-plan` | true | 工作日 15:48 | local | 2000 | 无 | 中 | 🟡降频 | HEARTBEAT 写已禁用但 manifest 仍启用；深研队列有价值，但不必每日无条件跑。 |
| `theme-strength-daily` | true | 工作日 15:45 | local | 3000 | 无 | 中 | 🟢保留 | 主题强度和生命周期会影响后续主题研究，纯确定性且低风险。 |
| `research-dispatch` | true | 工作日 15:50 | local | 2000 | 无 | 中 | 🟢保留 | 研究任务确定性调度，是研究平面入口，适合盘后每日一次。 |
| `candidate-fsm-sweep` | true | 工作日 15:55 | local | 1500 | 无 | 中 | 🟢保留 | 清扫候选状态超时，防止观察池膨胀和过期信号污染。 |
| `news-monitor` | true | 工作日 09、11、15、20、22 点 | feishu_direct | 3000 | 无 | 高 | 🟢保留 | 宏观/产业/地缘触发直推，已从 7 次降到 5 次，保留现频率即可。 |
| `official-policy-watch` | true | 每天 08:03-22:53 每 10 分钟 | feishu_direct | 3000 | 无 | 高 | 🟡降频 | 一手政策源价值高，但全天含周末每 10 分钟过密；建议工作日盘中 15-30 分钟，晚间保留低频。 |
| `market-pulse-1314` | false | 工作日 13:14 | origin | 1000 | 无 | 低 | 🔴删除 | 已禁用、无下游，且与资讯/催化/资金流报告重叠。 |
| `market-pulse-1500` | false | 工作日 15:00 | origin | 1000 | 无 | 低 | 🔴删除 | 已禁用、无下游，收盘信息可由 `closing-triage` 覆盖。 |
| `news-monitor-intraday` | true | 工作日盘中每 15 分钟 | feishu_direct | 1800 | 无 | 中 | 🟡降频 | 已降频但仍与官方政策和催化触发重叠；建议 30 分钟或仅持仓/候选命中时推送。 |
| `institution-weekly` | true | 周六 10:00 | local | 3000 | 无 | 中 | 🟢保留 | 不是每日任务，周频归档合理，可作为机构证据背景。 |
| `event-calendar` | true | 周一 08:00 | feishu_direct | 2500 | 无 | 中 | 🟢保留 | 周一事件提醒直接推送，频率低且有助于周内风险准备。 |
| `performance-daily` | true | 工作日 16:10 | local | 2500 | `performance-weekly` | 高 | 🟢保留 | 推进 T+1/T+3 结算和绩效反馈，是策略闭环必需。 |
| `ledger-projector` | false | 工作日 09:40、15:40、16:40 | local | 3000 | 无 | 低 | 🔴删除 | 已禁用、无下游；若状态投影已由运行时上下文替代，可从 cron 清单移除。 |
| `performance-weekly` | true | 周日 10:00 | local | 3000 | 无 | 中 | 🟢保留 | 周频胜率统计频率合理，保留为研究门槛和策略反馈。 |
| `catalyst-trigger` | true | 工作日盘中每 30 分钟 | origin | 2000 | 无 | 中 | 🟡降频 | 盘中催化有价值但近期全部 blocked；建议修复运行状态后改为候选池存在且有 T1/T2 催化时触发。 |
| `snapshot-gc` | true | 工作日 17:20 | local | 2500 | 无 | 高 | 🟢保留 | 清理快照和产物，防止状态目录膨胀，频率合理。 |
| `industry-map-refresh` | true | 工作日 08:30 | local | 4000 | 无 | 高 | 🟢保留 | 候选发现依赖行业映射缓存，盘前刷新合理。 |
| `candidate-freshness-check` | true | 工作日 15:15 | feishu_direct | 2000 | 无 | 高 | 🟢保留 | 候选池健康异常告警，能及时发现主链缺档。 |

## 建议删除的任务

建议从每日/例行 cron 中删除或保持非安装状态；保留脚本供手工调用即可：

- `provider-health`
- `social-attention-preopen`
- `behavioral-finance-preopen`
- `social-attention-midday`
- `behavioral-finance-close`
- `market-pulse-1314`
- `market-pulse-1500`
- `ledger-projector`

说明：这些任务共同特点是无直接推送或已禁用、无硬下游、输出可被其他报告覆盖，删除后不应影响核心交易决策。若删除 `social-attention-preopen`，需保留 `candidate-preopen` 的 optional 依赖语义或同步清理引用。

## 建议降频或改触发的任务

- `global-evening`：改为每周或海外市场异常时触发。
- `company-event-opportunity-scan`：并入盘前简报，或改为每周加重大事件触发。
- `social-attention-close`：若仍要社会关注数据，只保留收盘版本并降频；否则同步删除下游 optional 引用。
- `hot-money-afternoon-checkpoint`：改为上午候选强信号时触发。
- `intraday-alert`：按 HEARTBEAT 的用户偏好，建议禁用；若保留，移出 `capital-flow` 硬依赖并仅对持仓/订阅触发。
- `hk-a-linkage`：改 optional，或盘前/收盘一次；当前 disabled 但被硬依赖，需要修正。
- `stock-intelligence-refresh`：按候选变化或每周刷新。
- `serenity-refresh-plan`：按研究到期/候选变化触发，不建议每日无条件启用。
- `official-policy-watch`：工作日盘中 15-30 分钟，晚间低频；周末可关闭或更低频。
- `news-monitor-intraday`：30 分钟或仅命中持仓/候选时推送。
- `catalyst-trigger`：修复 `blocked_state` 后改为有候选/催化时触发，或保留关键窗口而非全天每 30 分钟。

## 建议保留的任务

核心交易决策链：

- `morning-note`
- `global-preopen`
- `candidate-preopen`
- `preopen-intelligence-brief`
- `auction-snapshot`
- `auction-market-snapshot`
- `auction-finalize`
- `auction-intelligence-brief`
- `open-confirmation`
- `open-intelligence-brief`
- `capital-flow`
- `candidate-discovery`
- `four-dim-scorer`
- `portfolio-check`
- `closing-triage`
- `performance-daily`

支持性但必要的状态/研究/清理链：

- `hot-money-context`
- `hot-money-context-backfill`
- `hot-money-morning-checkpoint`
- `theme-strength-daily`
- `research-dispatch`
- `candidate-fsm-sweep`
- `news-monitor`
- `institution-weekly`
- `event-calendar`
- `performance-weekly`
- `snapshot-gc`
- `industry-map-refresh`
- `candidate-freshness-check`

## 优先整改清单

1. 先修正 enabled 与硬依赖冲突：
   - 要么启用 `four-dim-scorer`，要么从 `portfolio-check`/`closing-triage` 的硬依赖中移除并提供替代输入。
   - 要么启用 `hk-a-linkage`，要么把它设为 `candidate-discovery` 的 optional dependency。
2. 对齐 HEARTBEAT 与 manifest：
   - 如果继续尊重“主人明确不需要 intraday-alert”，应把 `intraday-alert.enabled=false`，并同步调整 `capital-flow.context_from`。
   - 如果社会关注度确实已禁用，应把三个 `social-attention-*` 的 manifest 状态与下游 optional 引用清理一致。
   - `serenity-refresh-plan` 需要决定是恢复为每日研究队列，还是按 HEARTBEAT 禁用/改触发。
3. 处理运行状态：
   - `job_runs.json` 显示多个高频任务全部 `blocked_state`，应先修复 `A_STOCK_STATE_HOME`/state identity 配置，否则继续讨论频率意义有限。
4. 清理低价值直推：
   - 优先收敛 `official-policy-watch`、`news-monitor-intraday`、`catalyst-trigger` 三类盘中高频推送，避免盘中噪音压过真正交易信号。

