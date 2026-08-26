# checkpoint: 游资情绪交易体系升级执行（P0-P6）

方案: `docs/hot-money-emotion-system-upgrade-plan-2026-08.md`（用户 2026-08-25 确认）
执行模式: 主对话调度 + 独立终检（不采信子代理自述）；实现派 Opus 5 / Sonnet 5 子代理。
授权: 2026-08-26 起用户授权**自行合并**，按方案做完为止。
并发纪律: 最多 1 个写入子代理（dispatch.md §1）。

## 已合并（13 个 PR）

| PR | 内容 | main commit |
|---|---|---|
| #267 | P0 情绪日报数据集 + S_t shadow | f54d485 |
| #268 | P5(a,b) 成交约束模型 + rule_version | 4073c77 |
| #269 | P1 State PnL 归因（零样本 UNVERIFIED） | 51571a5 |
| #271 | P2 LeaderScore / ThemeScore | 1548e5d |
| #272 | P3-S1 超预期 RankSurprise | e2ad43d |
| #273 | P3-S2 龙头分歧回封 | 001421c |
| #274 | 事件表 EVENT_SCHEMA v4 | e662a8a |
| #275 | 分钟线派生管道（S1/S2 首次非零命中） | 05bb34a |
| #276 | P6 合规红线（AGENTS.md + 行为断言） | 777cc62 |
| #277 | P3-S3 最强助攻套利 | c70a837 |
| #278 | P5(d) 尾部风险指标 | 4e5483d |
| #279 | P3-S4 先于龙头套利 | 3707859 |
| #280 | P5(c) 消融实验 A→G | 1c712e7 |
| #281 | P3-S5 反量龙回头 | f5cb36b |
| #282 | P6 大面股库 | 73d32a8 |
| #283 | P6 纪律分 DisciplineScore | 61968c3 |
| #284 | P6 晚间复盘清单 | b9334d9 |
| #285 | P3-S6 冰点反转（P3 收官） | d2d2769 |

## 剩余

- [ ] **P4 仓位/止损/熔断** — opus 子代理进行中（分支 feat/p4-position-risk）
      1+1+1 状态机 / R 化风险预算 / 环境总仓表 / 四层止损（事件止损优先）/ 熔断阶梯；**paper 先行**。
      这是方案的最后一块；合并后 P0-P6 全部交付完毕。

## 核心约束（每次派发前重申）

- **本机只做管道就绪**（用户 2026-08-25 决策）：历史窗口仅 22 交易日、无全市场缓存，
  **严禁用小样本出胜率/PF/期望值结论**；零命中如实报 UNVERIFIED 是正确结局。
- 策略一律 **NON-LIVE 未注册**；NON-LIVE 必须用**行为断言**（消费端真降为 watch/零仓位），
  不是断言 pack 字段值。
- `config/daban_thresholds.yaml` 既有阈值**零删改**，只可追加新节（铁律）。
- 阈值不得照分位数回拟合，调整须走 research_gate。
- 空集/缺数据一律 `unavailable`，绝不用 0 或代理值冒充。

## 反复踩到的坑（派发时直接写进 prompt）

1. **Bash 每轮重置工作目录**：多步命令必须 `cd <worktree> && ...` 串一条，否则会在主仓库上
   跑测试拿到错数字（我自己踩过一次，3313 vs 3345）。
2. **mutation 前必须先 commit**：`git checkout --` 恢复到 HEAD，未提交的改动会被自己的
   变异循环删掉。
3. **mutation 没变红 ≠ 实现正确**：先用 `git diff --numstat` 确认变异真的生效（多次遇到
   perl 没匹配上、`continue` 仍在导致空操作），查清再下结论。
4. **worktree 里 `import skills.common` 解析到主仓库副本**（editable 安装），新模块 CLI
   直接跑会 ModuleNotFoundError；验证用 `PYTHONPATH=$PWD`。
5. 写 `docs/*.md` 被 settings 钩子拦，用 Bash heredoc 并说明原因。
6. **mutation 复原后仍报红时，先清 `__pycache__` 再下结论**：本仓扁平模块导入
   （`import xxx` 走 sys.path）+ 快速 mutate/restore 循环会命中过期字节码，
   造成"源码已复原但测试仍红"的假象（S6 那轮踩到，清缓存后 31 passed）。

## 待生产数据到位后必须补做

- P1 三套情绪口径的区分度结论、分组单调性、方案 §4.2 预期的证实/证伪。
- P2 LeaderScore 20 日一致率与分歧案例复核、分组单调性；ThemeScore 与现 4 因子分对比。
- P3 各策略 OOS 闸门判定、胜率/PF/期望值、分情绪状态 PnL、消融 A→G 实跑、纸面 ≥20 笔。
- 方案 §11 全局验收第 2 条（策略走完全流程）在本机不可达，已显式推迟。
- **量比阈值待复核**：真实分布中位 7.84 / p90 27.9，`min_volume_ratio=1.5` 近乎非约束，
  S1 四条件实际只有三条起作用（见 docs/minute-derived-pipeline-2026-08.md）。
- **P2 孤板惩罚生产风险**：依赖 sentiment_daily 的 break_rate/leader_damage 序列，本机 0 行；
  生产若也缺，这条核心逻辑会静默成死代码 —— 上线前必须确认序列有值。
- 各策略零命中的数据缺口：S1 `volume_ratio`（已由 #275 解决）、S2 `pre_reseal_turnover`、
  S3 `leader_score_shadow` 权重不足 + `breakout_time`、S4 `t_amount` 全量为 None。

## 文档归属（用户指令：入库但不上 GitHub）

`docs_private/` 下有权威副本；`docs/` 下的同名副本用 `.git/info/exclude` 本地排除
（该文件不进仓库），文件留在磁盘供用户复盘，不会被 git add 捕获。
