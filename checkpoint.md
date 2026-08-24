# checkpoint: issue #260 局部板块共振解耦

方案: `.omx/plans/issue-260-local-theme-resonance-fix.md`
基线: `origin/main@37dcf2c`（已同步本地 main）

## 阶段状态

- [x] 0. 方案与现状核对 — Explore agent 核实：10 项根因结论、行号、config/ledger 结构与 37dcf2c 完全一致，无漂移
- [x] A. 拆分门禁与盘前观察面 — 已完成并测试通过
  - [x] A.1 `hot_money_selection.build_market_gate()`：market_gate_v2，data_ready/retreat/新风险三轴分离；`_timing_reasons`/`daban_ready` 聚合行为不变
  - [x] A.2 新文件 `skills/common/local_theme_resonance.py`：`build_local_theme_gate()` 纯函数 + `can_upgrade()`；17 个单测
  - [x] A.3 `build_sector_leadership()` 输出每板块 `local_theme_gate`（独立 `local_theme_evidence_types`，不污染既有 `evidence_types`/`evidence_count`）；`apply_leader_identity()` 投影到候选
  - [x] A.4 `candidate_discovery.run_discovery()` 输出 `market_gate`/`local_theme_candidates`/`counts.local_theme`；market_gate=blocked 时局部池恒空；D0 恒 observed（build_local_theme_gate 内部封顶）
  - [x] A.5 `stage_intelligence.preopen_digest()` + `market_intelligence_brief.py` 展示 market_gate 三态 + 局部主题观察列表
  - [x] A.6 修复 `_preopen_no_candidate_line()` 的嵌套温度读取 bug（此前恒读 `timing.tier` 顶层字段=None，恒判定"证据未就绪"）
  - [x] `config/candidate_selection.json` 新增 `local_theme_resonance` 节（enabled=true, conditional_trade_enabled=false）
  - 验证：ruff 全绿；maintainability budget 全绿（发现 long_function 超标已修复：拆出 `_structural_breadth/_core_strength/_diffusion_evidence/_resolve_resonance_status/_resolve_execution_risk`）；全量 pytest 3011 passed（+3 pre-existing 环境相关失败，与本次改动无关，已用 git stash 验证）
- [x] B. 竞价二次确认 — 已完成并测试通过
  - [x] B.1 `auction_collector.watch_pool_codes()` 并入 `local_theme_candidates` 成员，使其获得 09:15-09:25 深池抓取；普通 research_candidates 不获得此待遇
  - [x] B.2/B.3 `candidate_pipeline.rank_auction_shortlist()` 新增 `_build_conditional_candidates()`：按 D0 lineage 白名单（`local_theme_candidates`）分组，用 09:25 新鲜 `board_status` 重算 strong_member_codes/breadth/limitup_cluster（不沿用 D0 快照），sector_flow/theme_member_confirmed 因无分钟级更新源而沿用 D0；确认通过写 `conditional_candidates`（`admission_state=conditional_pending`, `execution_risk_status=pending`），未通过留在重建的 `local_theme_candidates`；镜像/降级质量的行从强势成员计算中剔除
  - [x] B.4 采集为空走 `_degraded_finalize`（已补 `local_theme_candidates=[]`/`conditional_candidates=[]`）；镜像盘口/量能缺失单独测试覆盖 blocked 结果
  - 既有 `research_only` fence 已保证 execution_candidates/conditional_candidates 互斥，无需新增逻辑（已用专门测试验证普通研究票不能借局部路径绕过）
  - 验证：ruff 全绿；maintainability budget 全绿（同样发现并修复一处超长函数，拆成 4 个小函数）；tests/test_candidate_pipeline.py + tests/test_auction_collector.py 全绿（新增 8 个用例）；全量 pytest 3018 passed
- [x] C. 09:35 开盘确认与最终策略门禁 — 已完成并测试通过
  - [x] C.2 `decision_policy.evaluate_decision()` 新增 `participation_scope` 参数：`local_theme_only` + 请求 buy/add 时集中封顶为 conditional_buy；`requested_action`/guardrail 用原始请求值做审计
  - [x] C.4 `recommendation_audit.VALID_ACTIONS` 加入 `conditional_buy`；`record_recommendation`/`position_guidance` 新增 `participation_scope` 透传；`_cap_local_theme_position()`：仓位=min(局部配置上限,局部试验预算)，金额与封顶后 pct 一致（不是恒零——只有预算为0才是shadow）；`signal_ledger.TRADE_ACTIONS/SETTLEABLE_ACTIONS` 确认不含 conditional_buy，已加回归锁定测试
  - [x] C.1 `open_confirmation.py`：新增 `build_local_theme_signals()`/`_reconfirm_open_sector_gate()`/`_open_local_theme_evidence()`，从 `shortlist_result.conditional_candidates` 用 09:35 真实开盘 tradeability(limit_up/limit_up_sealed 视为强势) 重算 breadth/limitup_cluster，risk_reviewed=True + 公告硬风险/可成交性作为 risk_hard_block；复用 `evaluate_open_confirmation` 同款 `_open_execution_controls`(PIT契约)；接入 `_apply_policy(participation_scope="local_theme_only")`；结果并入 `signals` 走既有 ledger/monitor/recommendation_audit/candidate_lifecycle 循环
  - [x] C.3 局部路径动作上限固定 conditional_buy — 发现并修复 `_apply_policy` 原有 bug：只在 decision∈{avoid,watch} 时才同步 result.decision，导致 policy 内部已封顶为 conditional_buy 但顶层 decision 仍显示未封顶的 buy；已加 elif 分支同步
  - [x] C.6 `local_theme_conditional_trade_enabled=false` 时只输出 watch；已补 shadow_decision：结构+风险已就绪但开关关闭时，`_build_local_theme_signal` 额外算一次"若开启会怎样"的 counterfactual（decision/reasons/would_be_position_pct），挂在同一 signal 的 `shadow_decision` 字段，不影响真实输出；结构未就绪或开关已开时 `shadow_decision=None`
  - [x] C.8 顶层 `research_only=True` 只清空普通 shortlist 信号，不清空 conditional_candidates——端到端测试验证
  - [x] C.9 审计调用链闭合验证：open_confirmation→recommendation_audit→position_guidance→signal_ledger 全程 participation_scope 透传，端到端测试验证 settleable_signal=False、trade_id 缺失
  - [x] C.10 风险顺序：sector 级重算强势成员时已剔除 quality-unavailable/mirror 行（auction 阶段）与 tradeability/announcement 硬风险（open 阶段）后再判定 confirmed；**场景矩阵验收时发现并修复真实 bug**——`_reconfirm_open_sector_gate` 原先把任一成员的 `risk_hard_block` 直接 OR 进板块级 `execution_risk_status`，导致单票公告硬风险会把整个板块的其他 3 只无风险成员一起阻断成 watch；已改为先剔除风险成员再计算结构，板块级 `execution_risk_status` 不再被单票风险污染，个体风险仍由 `structurally_ready` 单独挡住该票自己
  - 验证：ruff 全绿；maintainability budget 全绿；受影响 11 个测试文件共 348 个测试全绿（含新增约 30 个 local_theme 专项测试）；调试中发现并修复 3 个真实设计缺陷：(1)`_apply_policy` decision 同步遗漏 (2)`_cap_local_theme_position` 金额恒零错误 (3)local theme 候选缺 directional_eligible/PIT 字段导致 transport_lower_trust 误判；全量 pytest 3042 passed（3 个既有环境失败无关）
- [x] D. 盘中异动语义一致 — 已完成并测试通过
  - [x] D.1 `load_sector_watchlist()` 同时读取同日 `shortlist`/`local_theme_candidates`/`conditional_candidates`，标注 `source`；execution 身份优先于 local_theme（同票双重出现时不被覆盖）；手工 tombstone 通过 `runtime_targets.cancelled_stock_codes()` 排除，不被 local_theme 发现重新激活
  - [x] D.2 `detect_sector_acceleration()` 新增 `participation_scope`：板块成员全部来自 local_theme 时标 `local_theme_only`，混合来源仍是旧语义；`action` 恒为 `watch`，不因来源改变
  - [x] D.3 数据降级/成员不足仍走既有停发+显式告警路径（未改动，仍绿）
  - 验证：ruff 全绿；maintainability budget 全绿；tests/test_intraday_monitor.py 19 个测试全绿（新增 6 个）；全量 pytest 3048 passed
- [x] E. 场景矩阵验收（方案§5 表格 7 行）— 已逐行补充端到端测试
  - Row1 restricted+无共振：隐含由"无 observed/confirmed 板块 → local_theme_candidates 为空"覆盖，无需额外造数据
  - Row2/3 restricted+多票共振→confirmed 条件候选：`test_restricted_market_with_observed_sector_produces_local_theme_candidates`(candidate_discovery) + `test_build_local_theme_signals_confirms_conditional_buy_when_enabled`(open_confirmation)
  - Row4 单票脉冲→不升级：`test_single_stock_pulse_never_enters_local_theme_candidates`(candidate_discovery)
  - Row5 stale/degraded→blocked：`test_local_theme_degraded_auction_quality_blocks_conditional_confirmation`(candidate_pipeline) + market_gate blocked 系列(hot_money_selection/brief)
  - Row6 公告/可成交性失败→watch+仓位0：`test_build_local_theme_signals_blocks_member_with_announcement_hard_risk`(open_confirmation，本次新增，过程中挖出并修复了 C.10 的真实 bug)
  - Row7 open+板块共振→旧路径：`test_open_market_does_not_use_local_theme_path`(candidate_discovery) + `test_blocked_market_never_produces_local_theme_candidates`
  - 额外新增 `checkpoint.md` 之外的测试：tests/test_candidate_discovery.py +4、tests/test_open_confirmation.py +4（含 2 个修正后的单元测试）
- [x] F. 最终验证门槛（方案第6节命令全跑，含 E 阶段新增测试后复跑）— 本地 3.13 全绿：全量 pytest 3056 passed(+3 与本次无关的既有环境失败，已用 git stash 核实) / validate_cron_manifest OK(67 jobs) / ruff 全绿 / compileall 全绿 / maintainability budget 全绿 / git diff --check 全绿。**CI 3.10 矩阵尚未跑**——本地 3.13 全绿只代表矩阵一半，按方案第6节要求仍需等远端 3.10/3.13 CI 与 CodeQL 全绿才算最终验收通过。

## 提交记录

- `b36a9b8` feat(daban): 市场冰点与局部板块共振解耦（issue #260）—— A/B/C/D 四阶段主体
- `1e1e9e0` feat(daban): 补齐 local_theme_only 的 shadow_decision（issue #260 C.6）
- `ba3ef21` fix(daban): 单票公告硬风险不再阻断整个局部板块（issue #260 C.10）—— 场景矩阵验收时发现的真实 bug

## 四阶段总结（供下一次继续时快速定位）

方案 A-D 全部落地，核心新增：
- `skills/common/local_theme_resonance.py`（新文件）：`build_local_theme_gate()` 纯函数状态机 + `can_upgrade()`
- `hot_money_selection.py`：`build_market_gate()`（market_gate_v2）+ 板块级 `local_theme_gate` 投影
- `candidate_discovery.py`：`local_theme_candidates`（D0 观察池）
- `candidate_pipeline.py`：`_build_conditional_candidates()`（09:25 二次确认 → `conditional_candidates`）
- `auction_collector.py`：`watch_pool_codes()` 并入局部观察成员
- `decision_policy.py`：`participation_scope` 集中封顶 conditional_buy
- `recommendation_audit.py`：`conditional_buy` 合法动作 + 仓位上限隔离
- `open_confirmation.py`：`build_local_theme_signals()`（09:35 第三次确认，含真实 PIT/风险复核）
- `intraday_monitor.py`：盘中告警识别局部主题来源

**已知未做（超出本次范围，非安全缺口）**：
- capital_concentration 证据类型（成交额/换手扩散）——代码库无现成信号源，local_theme_resonance.py 文档中已声明未实现
- 场景矩阵表格（§5）与 CI 3.10 远端矩阵尚未逐项验收

## 关键约束（不可违反）

- 不改 market_temperature.py 五档计算/冰点子状态/退潮检测
- 不降低现有个股/组合/公告/PIT/可成交性/退出协议门槛
- execution_candidates / local_theme_candidates / conditional_candidates 三者互斥
- conditional_buy 不进 TRADE_ACTIONS/SETTLEABLE_ACTIONS
- local_theme_resonance.enabled / local_theme_conditional_trade_enabled 默认均为可关闭开关，交易开关默认 false
- 阈值只放 config/candidate_selection.json 的 local_theme_resonance 节，不硬编码股票/行业

## 备注

- 首次核对：本地 main 落后 origin 一个提交（#259），已 fast-forward 同步。
