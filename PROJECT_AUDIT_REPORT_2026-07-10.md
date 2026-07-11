# A 股 Agent System 全面审查报告

> 项目：`Eleven1111/a-stock-agent-system`
> 审查日期：2026-07-10（Asia/Shanghai）
> 代码基线：`origin/main@8e59b53a96ffad541396e20e42a8081a502ecbe1`
> 审查性质：代码、文档、测试、Git 历史、GitHub PR/Issue/Actions、金融工程、Agent 工程、运行治理与安全的只读审查
> 重要边界：本报告没有登录另一台部署机，无法验证 OpenClaw/Hermes 的实际 commit、运行配置、共享状态、真实成交与真实收益。

---

## 1. 执行摘要

### 1.1 一句话结论

这是一个**工程治理骨架相对完整、但选股择时有效性尚未被证明**的 A 股研究与风险提示系统。它当前适合继续做数据采集、研究编排、盘前/盘中监控和人工决策辅助；不适合被当作已经验证的 alpha 系统，也不适合在无人复核情况下把“立即入场”“必须减仓/清仓”等方向性文案直接转化为实盘动作。

本次审查没有发现“项目已经具有稳定盈利能力”的可复验证据。相反，仓库自身的研究材料诚实地记录了：Chanlun 四类信号均未通过门禁；回调策略只有 10 个 OOS 样本且存在幸存者偏差；旧版追板逻辑曾得到负面结果；要求累计至少 60 个真实交易日快照的组合级 OOS Issue 在一天后、所有验收项未完成的情况下被关闭。工程测试通过不能补足这部分证据缺口。

### 1.2 总体判定

**审查判定：REQUEST CHANGES / 暂不具备“可靠选股择时系统”的生产放行条件。**

这里的“不放行”不是说项目不能运行，也不是说所有策略必然无效，而是说：

1. 当前绩效反馈与部分回测存在时间口径、成交假设和统计门禁问题；
2. 若干手工/批量入口能够生成绕过完整政策闸门的方向建议；
3. 数据缺失、弱市、持仓取价失败等场景仍存在 fail-open 或误报“正常”的路径；
4. GitHub CI、评审、分支保护和安全治理基本没有形成独立约束；
5. 部署机真实状态、策略注册表、共享状态、重复投递和实际 P&L 不在仓库证据中。

### 1.3 成熟度评分

评分不是收益预测，而是对当前仓库可验证成熟度的主观工程评级。

| 维度 | 评分 | 结论 |
|---|---:|---|
| 架构与治理骨架 | 3.5 / 5 | DAG、不可变快照、策略注册、信号账本、T+1、推荐质量闸门的方向正确 |
| 自动化测试 | 3.5 / 5（本地） | 1394 项通过，但关键问题多为“测试接受了错误业务语义” |
| GitHub 工程治理 | 1.0 / 5 | 231 次 Actions 无一次成功，零评审，main 无保护，bus factor 为 1 |
| 数据血缘与 point-in-time | 2.0 / 5 | 有快照和哈希，但历史补跑、跨源 fallback 与复权血缘仍不完整 |
| Agent 工程 | 2.5 / 5 | 研究总线和证据包有基础，但 LLM 分数缺乏证据绑定、版本与复核门禁 |
| 回测与统计严谨性 | 2.0 / 5 | 有成本/T+1 等机制，但仍有前视、不可成交假设和自我声明式 OOS 门禁 |
| 选股择时有效性证据 | 1.5 / 5 | 现有材料无法证明稳定、可交易、成本后 alpha |
| 组合风险与估值 | 2.0 / 5 | 有仓位规则，但行情缺失和行业字段缺失可使风险暴露失真 |
| 双运行时运维 | 2.0 / 5 | 有生成器与审计脚本，但无法从仓库证明两台驱动实际一致、无重入、无重复投递 |
| 无人复核实盘适用性 | 1.0 / 5 | 当前不建议；项目合同本身也明确是决策支持而非自动交易 |

### 1.4 最优先的七个阻断项

1. **重建真实的策略验收台账。** 重新打开组合级 OOS 验收，禁止用手工关闭 Issue 代替数据积累和证据 artifact。
2. **修正绩效反馈。** 不能再用信号日收盘价结算 09:35 才形成的信号；必须使用当时可观察、可成交的价格，并计入完整成本、涨跌停与无法成交。
3. **封住方向性旁路。** `four_dim_scorer`、批量 scorer、日报 JS 等入口必须通过同一套公告、数据质量、可交易性、价格计划、组合风险和策略注册闸门。
4. **修正风险 fail-open。** 行情取不到、市场温度未知、行业未知、弱市没有可交付候选时，都必须显式 `unknown/blocked`，不能等价于“正常”或自动补出可行动标的。
5. **恢复有效 CI 和独立合并约束。** 当前 GitHub Actions 从未成功；在 CI、required checks 和最少一名独立 reviewer 生效前，不应把 main 视为持续验证的发布分支。
6. **切断未经校准的 LLM 分数到强制退出动作。** 普通深研低分只能触发 review；“必须清仓”应绑定可验证的 hard-risk 事件和复核签名。
7. **做一次部署环境验收。** 在 OpenClaw/Hermes 实机上确认同一 commit、同一 `A_STOCK_STATE_HOME`、同一 manifest、无重复 job、无明文凭证、策略注册状态与真实运行 artifact。

---

## 2. 审查范围、方法与限制

### 2.1 实际阅读和检查的材料

- 最新远端 `origin/main` 的 343 个 Python 文件、152 个测试文件、92 个 Markdown 文件及配置/脚本；
- 根目录工程合同、README、架构硬化、交易生命周期、双运行时审计、策略研究门禁、组合研究协议等重点文档；
- 87 个 PR、6 个 Issue、GitHub Actions 全量运行摘要、仓库规则与安全配置；
- 当前 Git 历史、release/tag、贡献者集中度、依赖与发布元数据；
- 关键链路：provider → snapshot → DAG → candidate → scoring/research → policy → signal ledger → settlement/performance → delivery；
- 本地全量测试、ruff、manifest 校验、compileall、smoke test，以及一次隔离状态目录的实时数据源体检。

本地工作树的 `main` 在审查开始时比 `origin/main` 落后 19 个提交，并有用户自己的未跟踪文件 `docs/news-source-mcp-proposal-2026-07-09.md`。为避免污染用户现场，本报告的代码结论来自一个临时、干净、detached 的最新远端 worktree；没有修改或覆盖该未跟踪文件。

### 2.2 严重度定义

| 等级 | 含义 |
|---|---|
| P0 | 在信任方向建议、风险结论或策略有效性前必须修复；可能系统性污染决策或治理证据 |
| P1 | 高风险缺陷；可能在常见故障、历史回放、运行迁移或模型漂移中产生错误决策 |
| P2 | 重要但可排期；主要影响可维护性、可复验性、精度或运维质量 |
| P3 | 文档、元数据或体验问题；不会单独造成交易方向错误 |

### 2.3 本报告不能证明的事项

- 另一台电脑上的 OpenClaw/Hermes 是否运行最新 commit；
- 两个运行时是否真的共享同一个状态目录或分布式锁；
- 部署机 `strategy_registry.json` 中哪些策略已经被允许；
- 定时任务是否发生过重入、漏跑、重复推送、状态分叉；
- 真实成交、手续费、滑点、撤单、排队、停牌、分红送转与券商对账；
- 数据供应商许可、历史修订、复权一致性和长期可用率；
- Serenity/其他 agent 的实际模型、提示词版本、引用证据和人工复核记录；
- 任何真实盈利声明。仓库没有足够的原始 point-in-time 数据与可复验策略 artifact 支持这类结论。

---

## 3. 做得好的部分

### 3.1 工程边界总体方向正确

1. `skills/common/strategy_registry.py:63-98,111-148,174-200` 默认拒绝未注册策略，并在 live 使用时重新核对证据 artifact 与哈希。这是正确的“研究平面和生产平面分离”思路。
2. `skills/common/market_snapshot.py:74-136` 使用内容寻址、不可变快照和篡改检查，比直接依赖不断变化的 provider 返回值更适合审计。
3. `scripts/run_agent_dag.py` 和 manifest 把定时运行收敛到 run-scoped DAG；必需依赖失败时阻断下游，而不是默默输出中性结果。
4. `skills/common/signal_ledger.py` 把 canonical event 与派生 JSON 缓存区分开，方向上比多个互相覆盖的“当前状态文件”可靠。
5. `skills/common/state_store.py` 已有文件锁、临时文件和原子替换；单文件更新的基本机制是合格的。

### 3.2 A 股基础规则不是只写在文档里

- `skills/common/a_share_rules.py` 对交易日历覆盖不足采用 fail-closed，而不是简单把工作日当交易日；
- `skills/stock-triage/scripts/portfolio_manager.py:413-430` 对 lot 执行 T+1 锁定，阻止当日买入后卖出；
- 推荐质量层包含公告扫描、方向字段与不可交易状态的阻断；
- 组合回测已考虑 100 股手数、基础佣金/印花税/滑点、T+1 和一字跌停延迟退出等现实约束。

这些机制不能证明策略赚钱，但显著降低了“把美股/币圈的交易假设直接套到 A 股”的风险。

### 3.3 项目对负面研究结果有一定诚实度

- `docs/chanlun-gate-evaluation-2026-07.md:54-90` 明确记录四类 Chanlun 信号均为 `blocked/failed`，并保持零权重；
- `docs/pullback-strategy-gate-evaluation-2026-07.md:4-6,46-62` 承认 OOS 仅 10 笔及 20 只股票带来的幸存者偏差，没有注册 live 策略；
- Issue #28 的讨论承认旧追板逻辑结果为负，且历史 alpha 研究缺少可复验证据。

这说明作者并非一味美化结果。真正的问题在于：这些谨慎结论还没有在所有运行入口和 GitHub 治理上形成同样刚性的约束。

---

## 4. 关键发现总表

| ID | 等级 | 领域 | 发现 | 直接后果 |
|---|---|---|---|---|
| F-01 | P0 | 策略治理 | 组合级 OOS 的 60 日验收 Issue 未完成却被关闭 | “是否盈利”这一核心未决项从风险台账消失 |
| F-02 | P0 | 绩效反馈 | 09:35 信号用当日收盘价作为入场基准 | 前视污染收益与策略恢复/停用反馈 |
| F-03 | P0 | 回测 | 开盘涨停、盘中开板时按开盘价成交 | 使用全天信息决定开盘成交，缺少排队/容量模型 |
| F-04 | P0 | 决策闸门 | 四维评分/批量评分和日报可绕过完整政策检查 | 输出“立即入场”等文案但未完成强制风控 |
| F-05 | P0 | 风险管理 | 持仓取价失败可被遗漏且最终提示“持仓正常” | 暴露消失、权重失真、故障被误报为安全 |
| F-06 | P0 | 市场状态 | 极弱市场将 `research_only` 强制改成 `deliverable_watch` | 风险最高时为避免空报告而补出行动对象 |
| F-07 | P0 | CI/治理 | 231 次 Actions 运行零成功，零评审、main 无保护 | main 的合并不受自动或独立质量门禁约束 |
| F-08 | P1 | Agent 风险 | 无证据绑定的深研分可触发“必须清仓” | 幻觉、过时报告或评分漂移被升级为强动作 |
| F-09 | P1 | 组合风险 | 新建持仓不保存行业，40% 行业上限通常看不见既有持仓 | 同板块集中度可被系统性低估 |
| F-10 | P1 | 数据质量 | 市场上下文/温度缺失可映射为 neutral 和满倍率 | 外部数据故障被解释成“无风险” |
| F-11 | P1 | PIT/血缘 | 历史 `asof` 未贯穿 provider，fallback 后仍硬标 Tencent | 历史补跑可混入未来数据，来源与复权不可审计 |
| F-12 | P1 | 结算偏差 | 未解决信号可长期留在 pending，统计只看已解决样本 | 停牌/退市/数据失败等困难样本可能从统计中消失 |
| F-13 | P1 | OOS 统计 | 规则锁定、运行次数、未改规则等字段由程序固定自报 | 门禁可“自证通过”，不构成防过拟合证据 |
| F-14 | P1 | 安全/隐私 | OpenClaw 生成器把运行时 API key 放入命令；旧敏感材料未清史 | 凭证出现在进程/日志；私密历史不适合直接公开 |
| F-15 | P1 | 双运行时 | 审计脚本 job 身份映射和 `clean` 判定不完整 | 可能把未注册/状态失败或身份错配报告为干净 |
| F-16 | P1 | 账本完整性 | ledger 对损坏 JSONL 静默跳过，跨文件更新非事务 | canonical 事件可能无声丢失，现金/持仓/账本分叉 |
| F-17 | P1 | 供应链/网络 | 全局禁用代理、忽略 Retry-After、关键行情使用明文 HTTP | 部署环境兼容性、限流恢复和传输完整性下降 |
| F-18 | P2 | Agent 证据 | prompt 直接拼接外部证据，支持引用只验非空，不验 artifact ID/hash | 提示注入、伪引用与不可复验结论风险 |
| F-19 | P2 | 评分校准 | 缺失催化剂仍保留 4.5 基准分；confidence 是覆盖率而非预测概率 | 分数和置信度容易被用户误解为统计把握 |
| F-20 | P2 | 可维护性 | 大函数、宽泛异常、91 文件修改 `sys.path` | 边界难以测试，错误上下文容易丢失 |

---

## 5. 金融工程与策略可靠性审查

### 5.1 当前证据不能回答“能否稳定赚钱”

要证明选股择时可靠，至少需要同时回答：信号是否在当时可获得、是否能成交、成本后是否有正期望、是否跨市场状态稳定、是否在真正锁定规则后的 OOS 中重复出现、是否具有足够容量和统计显著性。当前仓库没有一套满足这些条件的完整证据链。

| 策略/证据 | 仓库中的实际状态 | 本次判断 |
|---|---|---|
| Chanlun | 四类信号全部 `blocked/failed`，零权重 | 没有 live alpha 证据，正确地保持研究态 |
| 回调策略 | OOS 仅 10 笔；20 只股票；承认幸存者偏差 | 样本远不足，不可外推 |
| 旧追板逻辑 | Issue #28 记录负面结果 | 不能用后续功能增加掩盖旧证据 |
| 组合级 OOS | Issue #32 要求至少 60 个真实交易日，但第二天即关闭 | 验收未完成，必须重开 |
| 龙虎榜退出 | 来自单次 -25.2% 个案复盘后的阈值 | 可作提醒，不足以作确定性退出规则 |
| 四维评分 | 多个启发式因子直接计权；confidence 代表字段覆盖 | 不是经过校准的收益概率 |

### 5.2 F-01（P0）：组合级 OOS 验收被过早关闭

[Issue #32](https://github.com/Eleven1111/a-stock-agent-system/issues/32) 于 2026-06-21 创建，要求累计至少 60 个真实交易日快照、锁定策略/数据、计入成本并完成组合级 OOS。九项验收均未勾选，无评论、无关联 PR，却在 2026-06-22 被 owner 手工关闭。时间上不可能在一天内积累 60 个新的真实交易日。

这不是文档瑕疵，而是策略治理的核心缺口：项目最重要的“盈利能力是否通过 OOS”问题，被从开放风险台账中移除了。

**整改：** 重新打开同等约束的 Issue；由 append-only artifact 自动更新进度；关闭条件必须由可验证的交易日数、数据集哈希、规则哈希、git commit、成本模型、全部变体和一次性验收结果触发，而不是人工口头判定。

### 5.3 F-02（P0）：绩效反馈存在前视并会污染策略门禁

`skills/stock-triage/scripts/performance_tracker.py:72-132,201-252` 使用“信号日收盘价 → T+1 收盘价”结算。开盘确认信号在 `skills/daban-stock-picker/scripts/open_confirmation.py:603-621,669-701` 约 09:35 才生成；使用当日收盘价作为入场基准，相当于在信号形成时引用未来价格。账本里已有推荐价格，但结算没有使用它。

此外：

- 绩效没有完整计入佣金、印花税、滑点、涨跌停排队和无法卖出；
- 虽等待 T+3 再门控，实际仍使用 T+1 收盘收益；
- 仅 12 个观察、毛期望非负即可恢复策略，没有成本缓冲、置信区间和市场状态分层；
- `alpha_t1` 只是同期收益减沪深 300，不是估计 beta 后的 alpha。

这会形成错误反馈环：错误入场基准 → 错误收益 → 错误地启用/停用策略 → 进一步改变 live 输出。

**整改：** 以可验证的 `trade.executed` 或当时可观察且保守可成交的价格结算；信号收益和成交收益分开；扣除完整成本；记录未成交/停牌/跌停无法卖出的 censored outcome；策略恢复采用滚动 OOS、成本压力测试、置信区间和足够的独立成交簇。

### 5.4 F-03（P0）：涨停回测的成交假设不可执行

`skills/chanlun-backtest/scripts/portfolio_backtest.py:251-284` 用入场日完整 OHLC 判断开盘涨停后是否开板，随后在 `:434-445` 直接按开盘价成交。实测反例：前收 10 元，次日 11 元涨停开盘、盘中最低 10.5 元，回测记录在 11 元成交，没有拒绝项。

问题是：开盘时不知道当天最低价；即使后来开板，也不能推出开盘排队订单在 11 元成交。模型缺少队列位置、封单、成交量参与率与撤单过程。对打板策略而言，这会使“可交易收益”和容量结论失真。

**整改：** 使用分钟或逐笔 point-in-time 数据；引入排队量、参与率、成交概率和保守未成交情景；同时报告理论信号收益、条件成交收益和保守可实现收益。

### 5.5 F-13（P1）：OOS 和统计门禁具有自我声明性质

`skills/chanlun-backtest/scripts/portfolio_backtest.py:672-706` 固定写入 `rules_locked=True`、`reports_all_variants=True`、`oos_run_count=1`、`changed_after_oos=False`。这不是外部证据，只是程序自报。组合回测没有像 Chanlun 一样使用持久化 OOS 运行注册表。

同时：

- OOS 样本量按交易日收益数计算，持有现金的零收益日也可能扩充样本；
- `fdr_p` 直接复制原始 permutation p，并未执行真正的多重检验校正；
- 所谓 cluster bootstrap 实际是 IID bootstrap，未处理时间序列或股票簇相关性；
- artifact 在结果生成后才哈希，不能证明规则在看结果前已经锁定。

**整改：** 建立不可变 OOS registry，先提交规则/数据/切分哈希再运行；样本按独立交易或股票×周期簇计算；使用 block bootstrap/HAC，并对多变体做真实 FDR、PBO 或 deflated Sharpe；失败结果同样永久保留。

### 5.6 F-12（P1）：未解决样本导致潜在幸存者偏差

绩效跟踪只拉取有限窗口行情；缺失、停牌、退市或 provider 失败的信号可能长期留在 `pending`，统计只使用已经解决的 T+1 样本。困难样本因更难获得价格而更容易从统计中消失，这会造成典型的 attrition bias。

**整改：** 对每个信号设置明确终态：`settled`、`not_tradeable`、`data_missing`、`delisted`、`suspended`；门禁必须同时报告覆盖率与缺失机制，并对无法退出采用保守估值，而不是无限 pending。

### 5.7 F-19（P2）：评分和“置信度”尚未校准

`skills/stock-triage/scripts/four_dim_scorer.py:426-480,808-840` 在催化剂不可用时仍保留 4.5 的基准分；测试明确锁定这一行为，而 README 声称缺失维度会重归一化。技术分文档宣称 0–10，代码却允许负分。`confidence` 基于数据字段完整程度，并不是收益概率、准确率或校准后的置信区间。

板块动量、连板、封板资金、主力流向、社交注意力和 agent 深研分都能直接进入加权总分，但仓库没有展示这些因子的增量 IC、消融、稳定性和成本后组合 OOS。正面例外是 Chanlun 保持零权重。

**整改：** 把 `confidence` 改名为 `data_coverage_tier`；缺失维度应显式降级或阻断，不应给中性先验后继续高评级；所有新因子先只展示解释，再经 point-in-time 消融与组合 OOS 后计权。

### 5.8 交易规则与组合账本仍不是券商级模型

`skills/common/tradeability.py` 主要按代码和名称推断涨跌停幅度，未完整覆盖新股前五日、重新上市、逐日 ST 状态、价格笼子和方向相关可交易性。组合现金与 P&L 未完整处理佣金、印花税、过户费、分红、送转、配股和拆并股。

因此当前组合数字更准确的称呼应是“研究账面估算”，不是可与券商对账的净值。

---

## 6. 决策链、风险控制与数据可靠性

### 6.1 F-04（P0）：方向建议存在政策旁路

`skills/stock-triage/scripts/four_dim_scorer.py:611-628,774-923,1045-1196` 可直接生成 `immediate_buy`、`立即入场` 等文案；`skills/stock-triage/scripts/batch_four_dim_scorer.py:140-151` 把 S/A 且数据覆盖 medium/high 的结果列为 signals。这条路径没有证明经过公告、完整数据质量、可交易性、价格计划、组合风险、策略注册和 T+1 的统一政策闸门。

`skills/a-stock-daily-report/scripts/a-stock-report.js:85-150,235-256` 还会把按日涨幅排序的板块描述为资金流入/趋势，并固定建议 60%–70% 仓位；这不是来自组合风险引擎的结论。

**整改：** 所有用户可见方向标签必须来自唯一的 `policy_decision` artifact；scorer 只输出因子与解释，不输出买卖动作；日报不得硬编码仓位或把涨幅冒充资金流。

### 6.2 F-05（P0）：行情故障可被误报为“持仓正常”

`skills/stock-triage/scripts/portfolio_manager.py:492-503,531-674,730-750` 在持仓取价失败时可能不把该持仓计入总市值和风险告警；旧 `market_value` 还可能残留。若最终 alerts 为空，格式化输出会给出“无风控警报，持仓正常”。

这对真实风险管理是不可接受的：停牌、provider 故障或单票异常不应让风险暴露消失，更不能转译为安全结论。

**整改：** 使用带时间戳的 last-known-good 价格和保守 haircut；任一重要持仓无法估值时状态必须为 `valuation_unknown`，阻断新增风险并显式告警；总资产同时报告已估值和未估值暴露。

### 6.3 F-09（P1）：行业集中度规则对现有持仓失效

`skills/common/portfolio_policy.py:65-77` 只统计带 `sector`/`industry` 的现有持仓，而 `skills/stock-triage/scripts/portfolio_manager.py:346-357` 新建持仓时不保存行业字段，之后也没有补全路径。结果是多个同板块持仓可逐个通过 40% 行业上限。

**整改：** 成交建仓时保存带 provider、版本和日期的行业分类；行业未知 fail closed；进一步增加相关性、beta、风格暴露、ADV、流动性和组合波动约束。

### 6.4 F-06（P0）：极弱市场为避免空报告而“救回”候选

`skills/stock-triage/scripts/candidate_discovery.py:766-831` 在所有候选均被降为 `research_only` 后，把最高分的最多 5 只改成 `deliverable_watch`，注释目的就是避免报告为空、保留 actionable targets。它们之后还会被注册到监控列表（`:875-910`）。

这与 `skills/common/weak_market_delivery.py` 的契约和仓库 fail-closed 原则直接冲突。没有候选是合法结果，尤其在极弱市场。

**整改：** 删除该覆盖；研究候选单独放入 `research_only_candidates`，不得改写原门禁，也不得自动注册到 live 监控。

### 6.5 F-10（P1）：未知市场状态被当作中性

`skills/common/market_context.py:56-77` 将缺失/过期上下文映射为 `neutral`；`skills/common/market_temperature.py:19-20,180-186` 在温度数据缺失时允许新打板且仓位倍率为 1；candidate discovery 捕获任意上下文异常后也返回 `allow_new_daban=True`。

虽然某些下游（如 hot-money selection）有额外阻断，但这些可复用 helper 和手工入口仍是 fail-open，违反“外部数据失败不得解释为无风险”。

**整改：** 引入 `unknown/stale` 一等状态；新方向仓位默认阻断或大幅缩仓；只有完整、时效合格的市场状态才能恢复正常倍率。

### 6.6 F-11（P1）：历史 as-of 和数据血缘没有贯穿

- candidate discovery 没把 `asof` 传入上市天数过滤，默认用 `date.today()`；
- K 线 provider 的结束日期固定为今天；
- auction collector 可把当前实时行情写入任意 `--asof`；
- snapshot 不断言 `captured_at` 是否晚于交易日或策略阶段；
- 多源 K 线 fallback 丢弃 provider/复权信息，最终快照仍可能硬标 Tencent。

正常“当天跑当天”不一定触发，但历史补跑、故障恢复和回测快照会混入未来数据或错误血缘。跨源复权差异还会在公司行动日制造伪信号。

**整改：** 所有 provider 接口接受 `event_asof`，并断言最大数据时间不晚于它；每组 bars 保存 provider、复权类型、抓取时间和版本；replay artifact 与现场 snapshot 分开命名；跨源序列做公司行动日一致性测试。

### 6.7 F-16（P1）：账本损坏与跨文件一致性

`skills/common/signal_ledger.py:169-186` 对损坏 JSONL 静默跳过，之后备份同步可能把过滤后的流当作新真相；restore 只在主文件不存在时触发。portfolio/cash/ledger/monitor 的多个文件更新也不是一个事务。

**整改：** 任一 ledger 行损坏必须使运行 `blocked` 并保留原文件；增加 hash chain/sequence number；跨文件操作采用单一事件先写账本、再幂等投影，或使用 SQLite 事务；启动时做余额与 lot reconciliation。

---

## 7. Agent 工程审查

### 7.1 研究总线的优点

项目已经把“外部证据 → research task → evidence pack → synthesis”做成独立平面，并明确结构性规则变化先进入研究而不是直接改变 live 策略。这比把新闻原文直接拼进选股 prompt 更安全，也为后续审计留下了合理边界。

### 7.2 F-08（P1）：未经校准的 agent 标量可触发强制退出

`skills/serenity-investment-research/scripts/scorecard.py:46-73` 接受人工或 agent 的 1–5 分；`skills/common/deep_research_cache.py:155-199` 保存映射分、日期和报告路径，但不绑定原 scorecard 哈希、引用证据、模型/提示词版本或审核签名。`skills/common/exit_signals.py:214-233` 把 `deep_score < 5` 定义为必须减仓、`<3` 定义为必须清仓；`skills/stock-triage/scripts/portfolio_manager.py:599-611` 会发出红色强制告警。

更严重的是，缓存读取可以返回 `stale`，部分退出路径只取数值而忽略 freshness。一份超过有效期的 agent 研报也可能参与强制退出。

**整改：** 普通低分只能生成 `review_required`；强制退出必须绑定结构化 hard-risk（财务造假、退市风险、重大监管、不可交易等）和可验证一手证据；缓存记录 artifact hash、引用、模型/提示词版本、复核人/复核模型和 freshness；过期结果绝不产生方向动作。

### 7.3 F-18（P2）：提示注入和引用真实性

`scripts/expert_runner.py:140-175` 将角色提示与外部证据直接拼接，没有明确的“不可信数据”封装；`skills/common/research_bus.py` 主要检查支持引用非空，不核对引用是否对应真实 artifact ID/hash；synthesis 可把模型自己给出的 stance/confidence 用于推进结论。

由于研究平面目前不直接下单，风险低于交易执行系统，但它会污染深研分、候选解释和退出告警。

**整改：** 外部文本做结构化 data-only 封装；禁止其中指令改变 system policy；引用必须解析到 evidence pack 内的 immutable artifact；输出使用 schema validator；对强方向结论做独立反证 agent 和确定性 policy check。

### 7.4 Agent 可观测性仍缺关键版本

仓库没有统一记录每次研究使用的：模型 ID、模型参数、system prompt hash、tool 版本、证据集合 hash、重试路径和人工编辑差异。没有这些信息，就无法区分“策略变了”“数据变了”还是“模型漂移了”。

**整改：** 为每个 agent 产物增加 `model_run_manifest`，把模型、prompt、tools、输入 artifact、输出 schema、审核结果和 commit 全部哈希化；任何进入评分的产物必须可重放或至少可审计。

---

## 8. 代码鲁棒性、安全与可维护性

### 8.1 F-14（P1）：运行时凭证与历史隐私

`scripts/generate_openclaw_cron.py:215-284` 会把 `MIAOXIANG_API_KEY` 的实际值写进 `--command-env`，dry run 还会打印命令。凭证可能进入 shell history、进程列表、OpenClaw 存储和日志。脚本还硬编码 Discord 用户目标，形成隐私和可移植性问题。

另外，[PR #52](https://github.com/Eleven1111/a-stock-agent-system/pull/52) 删除了大量运行残留/私密材料，正文明确表示旧 commit 仍有敏感内容并需要 `git filter-repo`；当前 main 仍包含该 commit 的祖先历史。仓库目前是 private，这降低了暴露面，但**不能直接改成 public**。

**整改：** OpenClaw 只存环境变量名或受保护 secret reference，不存值；目标收件人来自部署配置；轮换所有曾可能暴露的凭证；公开前对镜像/远端做历史清理和 secret scan，并通知所有 clone 重新同步。

### 8.2 F-17（P1）：网络客户端与明文行情

`skills/common/http_client.py` 全局构造 `ProxyHandler({})`，会绕过部署环境的代理；虽然解析了 `Retry-After`，却没有按它等待。Tencent 等关键行情路径使用明文 HTTP，传输完整性依赖网络环境。

**整改：** 默认尊重系统代理，必要时按 provider 显式关闭；严格执行 Retry-After 与 jitter/backoff；优先 HTTPS；对无法 HTTPS 的源做响应签名/交叉源校验，并将其标为较低信任等级。

### 8.3 缓存、原子性与状态目录

`skills/stock-analyst/scripts/data_cache.py` 的 K 线缓存 TTL 没有真正执行，路径还硬编码到 `~/.hermes/data`，可绕过 `A_STOCK_STATE_HOME`；SQLite 使用 `synchronous=OFF`。这与双驱动共享状态合同不一致。

**整改：** 所有状态路径统一经 `skills/common/paths.py`；缓存键包含 provider/复权/asof/version；强制 TTL；关键状态至少 `synchronous=NORMAL/FULL`；对跨机部署使用真正的共享存储和 lease，而不是本地文件锁。

### 8.4 F-20（P2）：可维护性热点

静态结构扫描显示约 1,799 个函数中有 179 个超过 50 行、41 个超过 100 行；91 个文件出现 118 处 `sys.path` 修改，另有大量宽泛 `except Exception`。最大的候选发现、runner 和全市场监控函数同时承担获取、判定、状态写入和格式化职责。

这不等于必然有 bug，但会放大业务语义错误：异常被吞掉后很难知道是“没有信号”还是“数据失败”，也难以对 point-in-time 和 fail-closed 做单元隔离。

**整改：** 优先拆边界而不是增加抽象：provider adapter、纯判定函数、event write、presentation 分离；禁止业务模块修改 `sys.path`；宽泛异常统一映射为带 provider/stage/retryability 的 typed failure。

---

## 9. GitHub PR、Issue、CI 与项目治理

### 9.1 全量统计

截至审查基线：

| 项目 | 结果 |
|---|---:|
| 仓库可见性 | Private |
| PR 总数 | 87 |
| 已合并 / 未合并关闭 / 开放 | 85 / 2 / 0 |
| Issue 总数 | 6，全部关闭 |
| PR 作者 | 87/87 均为 `Eleven1111` |
| 合并者 | 85/85 均为同一账号 |
| PR review / approval / review request | 0 / 0 / 0 |
| PR 讨论评论 | 4，均来自 owner |
| Actions 运行 | 231；0 success，19 failure，212 startup_failure |
| main 保护 / ruleset | 无 |
| Dependabot / secret scanning / code scanning | 未启用 |
| bus factor | 1（多个 author 名称/邮箱仍指向同一人） |

这不是对个人能力的否定，而是说明 GitHub 没有形成独立控制。对金融决策系统而言，“作者自己写、自己验、自己合并”无法提供模型风险管理需要的第二道防线。

### 9.2 F-07（P0）：CI 从未成功

仓库有一个 Python 3.10/3.13 matrix workflow，配置上会运行 ruff、pytest、manifest 和 compileall；但 GitHub 上 231 次运行没有一次 success。早期 19 次为 failure，之后 212 次为 startup failure；[最新运行](https://github.com/Eleven1111/a-stock-agent-system/actions/runs/29063657701) 没有启动任何 job。[PR #93](https://github.com/Eleven1111/a-stock-agent-system/pull/93) 说明私有仓库 Actions 因 billing/quota 被锁定。

PR #93 还承认 PR #92 合入后留下 8 个测试失败，而 CI 没有发出有效阻断。PR #93 本身改动 24 个文件、增加 1,635 行，仍是零 review、零 status check 后合并。

**整改：** 先恢复 Actions 计费/额度并取得连续绿色运行；配置 main protection、required checks 和至少一名非作者 reviewer；若 GitHub 套餐不支持，至少使用受保护的本地 merge bot/独立验收人并上传签名验证 artifact。

### 9.3 PR 周期过短，不能视为正式评审

85 个已合并 PR 中，50 个在创建后 10 分钟内合并，12 个在 1 分钟内合并，中位合并时间约 357 秒。快速提交本身不是错误，但在零 review、CI 不工作的背景下，这种速度意味着 PR 主要是变更容器，不是审查门禁。

**整改：** 对金融语义变更设置最短检查清单：时间可得性、可成交性、成本、状态缺失、PIT、策略注册、回归测试、反例测试和 OOS 影响；PR 模板必须填写并由第二人确认。

### 9.4 Issue 台账不足以承载模型风险

6 个 Issue 全部关闭、零开放，与仓库中仍明确存在的 OOS、数据源和部署风险不一致。除 Issue #32 外，[Issue #88](https://github.com/Eleven1111/a-stock-agent-system/issues/88) 从单笔 -25.2% 交易复盘推导出新龙虎榜/退出纪律；它适合提出假设，不足以直接成为普适规则。

**整改：** 将 Issue 分为 `model-risk`、`data-risk`、`runtime-risk`、`security`；研究假设、验收证据和生产启用分开；失败/未完成项不得因“代码已写”而关闭。

### 9.5 发布与仓库卫生

- 只有一个 v1.3.0 release（2026-06-09），但 `pyproject.toml` 仍是 1.0.1，main 已有大量后续变更；
- README/pyproject 声称 MIT，但仓库没有 `LICENSE`；
- 没有 `SECURITY.md`、`CODEOWNERS`、`CONTRIBUTING`、依赖 lockfile 或 Dependabot；
- CI 只启用 Ruff E/F/W，未覆盖类型检查、安全扫描、依赖漏洞和 secret scan。

这些是 P2/P3，但在计划公开或让更多 agent 自动修改仓库前应补齐。

---

## 10. 双驱动运行与交付审查

### 10.1 架构合同是合理的，但部署事实尚未验证

仓库要求 Hermes/OpenClaw 在同机共享 `A_STOCK_STATE_HOME`，多机则必须有共享状态和跨机 lease。这个边界是正确的。问题在于：本仓库只包含 generator、manifest 和本地审计脚本，不包含另一台电脑的实际 runtime inventory、环境变量、lease backend 和运行 artifact。

### 10.2 F-15（P1）：dual-runtime audit 可能误报 clean

`scripts/dual_runtime_audit.py` 将 OpenClaw 的不透明 job ID 与 manifest 逻辑 job ID 直接比较，和 generator 实际按名称管理的方式不完全一致；`clean` 判定没有把所有注册/状态查询失败纳入阻断，重复检测主要看完成时间差，也不能证明两个 job 没有重叠执行。

**整改：** 使用稳定 `logical_job_id` 标签而不是平台内部 ID；任一 runtime inventory/state query 失败即 `audit_status=blocked`；从 run artifact 的 `batch_id`、开始/结束时间、lease owner 和 delivery id 检测重叠/重复；在真实 OpenClaw/Hermes 上做一次端到端演练。

### 10.3 实机验收清单

1. 两个运行时都打印 commit SHA、manifest hash、skill/config hash；
2. 验证同一 `A_STOCK_STATE_HOME`，若跨机则验证共享存储和 lease；
3. 导出两个 runtime 的 job inventory，按 logical ID 一一对应；
4. 选一个无害 job 同时触发，证明只有一个 owner、另一个被 lease 阻断；
5. 验证同一 batch 不会双推送，delivery ledger 能去重；
6. 检查进程参数、日志、cron storage 不含 API key；
7. 导出 strategy registry，确认未通过 OOS 的策略权重为 0；
8. 让一个 provider 故障，确认输出是 `blocked/degraded` 而不是中性/正常；
9. 对持仓 quote 失败做演练，确认阻断新增风险；
10. 将 artifact 打包并签名，作为部署验收证据。

---

## 11. 本地验证结果

所有验证均在干净的 `origin/main@8e59b53` 临时 worktree 执行。

### 11.1 静态与单元验证

```text
$ pytest -q
1394 passed, 1 warning in 38.75s
```

唯一 warning 来自 `py_mini_racer.MiniRacer.__del__` 的 unraisable exception，出现在 `tests/test_dynamic_cron_targets.py`；不是业务测试失败，但说明 JS runtime 清理路径不够稳定。

```text
$ python scripts/validate_cron_manifest.py
OK: 44 jobs (0 local, 44 external)

$ python -m ruff check .
All checks passed!

$ python -m compileall -q scripts skills
# exit 0

$ git diff --check
# exit 0
```

可选安全工具 `bandit`、`pip-audit`、`semgrep`、`gitleaks` 在当前环境不可用，因此不能声称这些检查通过。coverage 模块也不可用，本报告没有测试覆盖率百分比。

### 11.2 smoke test 的含义有限

`python scripts/smoke_test.py` 返回 13 passed / 0 failed，但该脚本把“命令未输出 JSON”计作带 warning 的 PASS，证明的主要是“不崩溃”，不是内容正确、数据新鲜或策略可靠。

### 11.3 一次实时数据源体检

使用隔离状态目录运行 provider doctor，结果为 `degraded`：Tencent quote、日 K、个股资金流、板块行情、北向、龙虎榜、涨停池、全 A spot 和公开财经新闻可用；同花顺行业目录报错，Serper 因缺 key 不可用。另一次 fallback smoke 中全 A、quote、日 K、分钟、资金流、龙虎榜等可用，板块资金流为空/错误。

这是 2026-07-10 单次网络快照，只说明当前机器某一时刻的可达性，不代表部署机状态、长期 SLA、数据许可或历史正确性。

实时运行四维评分时，催化剂不可用但仍得到总分 4.5、medium confidence 和方向性文案，技术分甚至为 -0.2。这直接验证了前述“缺失维度与评分口径不一致”不是纯理论问题。

---

## 12. 分阶段整改路线图

### P0：0–7 天，先阻止错误信任

1. 将所有 scorer/report 的方向动作降级为“研究结果”，统一通过 policy artifact 才能输出买卖措辞；
2. 删除弱市 `research_only → deliverable_watch` 覆盖；
3. 修正持仓 quote failure、行业字段和未知市场状态；
4. 修正绩效入场价格，暂停它对策略自动恢复/停用的影响；
5. 禁止 stale deep score 触发退出，LLM 低分只触发人工 review；
6. OpenClaw 生成器移除明文 key 和硬编码收件人，轮换相关凭证；
7. 恢复 GitHub CI，main 设置 required checks；
8. 重开组合级 OOS/model-risk Issue；
9. 在真实双运行时环境执行第 10.3 节验收并保存 artifact。

### P1：2–4 周，修复证据链

1. 建立 point-in-time provider API 和完整数据血缘；
2. 重写成交仿真，加入排队、未成交、容量和完整成本；
3. 建立 append-only precommit/OOS registry，规则先锁定、结果后揭示；
4. 统一 ledger 序列/hash chain，修复静默跳过和跨文件一致性；
5. 引入模型运行 manifest、引用 artifact 校验和 prompt injection 隔离；
6. 修正 dual-runtime logical job identity、lease 和 duplicate delivery 审计；
7. 对龙虎榜、情绪、板块动量、资金流等因子做消融与事件研究，验证前零权重。

### P2：1–3 个月，才开始讨论策略放行

1. 累计至少 60 个真实 point-in-time 交易日，并保证每个 artifact 可复验；
2. 使用足够的独立成交/股票/市场状态簇，而不是仅靠日收益行数；
3. 报告所有变体、失败结果、成本压力、容量曲线、换手、最大回撤和尾部损失；
4. 使用 walk-forward、block bootstrap/HAC、PBO/deflated Sharpe 和多重检验校正；
5. 先 shadow deployment，任何策略只做观察，不影响 live 排名；
6. 通过预先定义门槛后，再以小权重、人工确认方式灰度；
7. 用真实成交对账验证仿真误差，超出阈值自动退回 research-only。

---

## 13. 建议的正式放行门槛

| 门槛 | 必须满足的证据 |
|---|---|
| 工程门槛 | GitHub CI 连续绿色；required checks；独立 review；测试、ruff、manifest、compileall、security/dependency scan 全绿 |
| 数据门槛 | point-in-time 时间断言；provider/复权/version 血缘；覆盖率/SLA；故障时 fail-closed；跨源一致性 |
| 策略门槛 | 规则/数据/commit 预锁定；≥60 真实交易日且足够独立成交簇；所有变体；成本/容量/压力测试；统计校正 |
| 组合门槛 | 行业/风格/beta/流动性/相关性暴露；无法估值时阻断；券商成交与账本对账 |
| Agent 门槛 | 模型/prompt/tool/evidence hash；引用可解析；过期结果禁用；强动作需 hard-risk 证据与复核 |
| 运行门槛 | OpenClaw/Hermes commit/config/state 一致；跨机 lease；无 job 重叠；无重复推送；无明文 secret |
| 灰度门槛 | shadow 足够长；实际成交与仿真偏差在阈值内；异常自动退回 research-only |

在这些门槛完成前，建议保留以下用途：

- 数据收集与异常监控；
- 盘前/盘中信息摘要；
- 候选研究队列；
- 公告、交易日历、T+1、可交易性检查；
- 人工复核前的风险清单。

建议禁用或降级以下用途：

- 把 S/A 级、medium/high confidence 直接解释为可买；
- 自动生成“立即入场”“必须清仓”并让用户照单执行；
- 用当前 performance feedback 自动放行策略；
- 把当前回测结果当成实盘可实现收益；
- 在未清理 Git 历史和凭证前公开仓库。

---

## 14. 最终意见

这个项目不是“完全不可用”。恰恰相反，它已经积累了比许多个人量化项目更好的工程组件：明确的研究/生产分界、策略注册、不可变快照、T+1、DAG、信号账本、推荐质量检查，以及对部分负面研究结果的诚实披露。

但也正因为它已经能生成完整、专业、行动感很强的报告，最大的风险不再是程序崩溃，而是**系统以很有说服力的形式输出未经充分验证的结论**。1394 项测试证明当前实现与当前测试约定一致；它们不能证明这些约定符合真实成交、统计推断和模型风险管理。

专业且审慎的定位应当是：

> **当前版本是 A 股研究编排与风险提示平台，不是已经完成金融工程验证的选股择时引擎。**

下一阶段最有价值的工作不是继续增加策略、数据源或 agent 数量，而是修复时间可得性、可成交性、失败状态、证据注册和独立治理，把“不能证明”变成一套不会被代码、文案或流程绕过的硬门槛。只有在真实 point-in-time OOS、成本容量、双运行时和 shadow 实盘全部通过后，才适合讨论任何策略的受控放行。

---

## 15. 关键 GitHub 证据链接

- [项目仓库](https://github.com/Eleven1111/a-stock-agent-system)
- [Issue #32：累计真实快照后执行组合级 OOS 盈利能力验收](https://github.com/Eleven1111/a-stock-agent-system/issues/32)
- [Issue #28：旧追板/历史研究证据讨论](https://github.com/Eleven1111/a-stock-agent-system/issues/28)
- [Issue #88：单笔重大亏损复盘](https://github.com/Eleven1111/a-stock-agent-system/issues/88)
- [PR #93：修复 PR #92 遗留测试与最新退出规则](https://github.com/Eleven1111/a-stock-agent-system/pull/93)
- [PR #52：删除私密运行残留，但提示仍需清理历史](https://github.com/Eleven1111/a-stock-agent-system/pull/52)
- [最新 Actions startup failure](https://github.com/Eleven1111/a-stock-agent-system/actions/runs/29063657701)

---

## 16. 报告交付物的对抗性验证

本节的 `VERDICT` 只表示“报告中的主要引用、统计和反例已被复核”，**不表示被审查项目通过生产放行**。

### Check: 本地代码引用路径和起始行有效

**Command run:**

```zsh
report=/Users/na/na/Claudecode/a-stock-agent-system/PROJECT_AUDIT_REPORT_2026-07-10.md
refs=$(rg -o --no-filename '[A-Za-z0-9_./-]+\.(py|js|md|toml):[0-9]+(-[0-9]+)?' "$report" | sort -u)
print -r -- "$refs" | while IFS=: read -r filepath range; do
    if [ ! -f "$filepath" ]; then
      print "MISSING $filepath:$range"
      continue
    fi
    first=${range%%-*}
    lines=$(/usr/bin/wc -l < "$filepath")
    test "$first" -le "$lines" || print "OUT_OF_RANGE $filepath:$range total=$lines"
done
print "REFERENCE_COUNT=$(print -r -- "$refs" | /usr/bin/wc -l | tr -d ' ')"
```

**Output observed:**

```text
REFERENCE_COUNT=26
# 无 MISSING 或 OUT_OF_RANGE
```

**Result: PASS**

这一探测在初稿中实际找出了多处被简写成错误目录的文件引用；修正后重新执行才得到上述结果。

### Check: GitHub PR、Issue、Actions 聚合统计

**Command run:**

```zsh
gh api --paginate --slurp \
  'repos/Eleven1111/a-stock-agent-system/pulls?state=all&per_page=100' | jq -c 'add | {total:length, merged:([.[]|select(.merged_at!=null)]|length), closed_unmerged:([.[]|select(.state=="closed" and .merged_at==null)]|length), open:([.[]|select(.state=="open")]|length), authors:([.[].user.login]|unique)}'

gh api --paginate --slurp \
  'repos/Eleven1111/a-stock-agent-system/actions/runs?per_page=100' | jq -c '[.[].workflow_runs[]] | group_by(.conclusion) | map({conclusion:.[0].conclusion,count:length})'
```

**Output observed:**

```json
{"total":87,"merged":85,"closed_unmerged":2,"open":0,"authors":["Eleven1111"]}
[{"conclusion":"failure","count":19},{"conclusion":"startup_failure","count":212}]
```

**Result: PASS**

### Check: OOS Issue 的非 happy-path 状态

**Command run:**

```zsh
gh issue view 32 --repo Eleven1111/a-stock-agent-system \
  --json createdAt,closedAt,state,body \
  --jq '{createdAt,closedAt,state,unchecked:([.body|scan("- \\[ \\]")]|length),checked:([.body|scan("- \\[x\\]")]|length)}'
```

**Output observed:**

```json
{"checked":0,"closedAt":"2026-06-22T12:25:52Z","createdAt":"2026-06-21T00:18:18Z","state":"CLOSED","unchecked":9}
```

**Result: PASS**

该探测还纠正了初稿把 checklist 数量写成八项的错误；API 实际返回九项未完成。

### Check: 开盘涨停、盘中开板的成交反例

**Command run:**

```zsh
python - <<'PY'
import importlib.util, json
s = importlib.util.spec_from_file_location('bt_test', 'tests/test_portfolio_backtest.py')
t = importlib.util.module_from_spec(s); s.loader.exec_module(t)
p = t._payload(top_n=1)
p['bars_by_code']['600001'][1].update(open=11.0, high=11.0, low=10.5, close=10.8)
r = t.portfolio_backtest.run_portfolio(p)
print(json.dumps({'closed_trades': r['metrics']['closed_trades'], 'entry_price': r['trades'][0]['entry_price'], 'rejections': r['rejections']}, sort_keys=True))
PY
```

**Output observed:**

```json
{"closed_trades": 1, "entry_price": 11.0, "rejections": []}
```

**Result: PASS**

该结果不是回测“通过”，而是成功复现了本报告 F-03 所述的不可执行成交假设。

VERDICT: PASS
