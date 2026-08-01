# 缠论模块升级方案（2026-08-01）

> 依据：对 Vespa314/chan.py（pinned `429d6ed`，2026-06-25，MIT）与 tianquanchen/chanlun 的源码级对比，
> 以及本仓库 `skills/chanlun-backtest/scripts/chan_structure.py` 现状与
> `docs/chanlun-gate-evaluation-2026-07.md` 的门控评估结论。

## 0. 结论先行

1. **tianquanchen/chanlun 无独立学习价值**：它是 Vespa314/chan.py 的 2025-04-28 旧快照
   fork（README star 徽章仍指向 Vespa314；无任何独有文件；chan.py 主库此后 14 个月新增
   App/、AkshareAPI、`used_to_be_sure` 等演进）。本方案的萃取对象只有 chan.py 一个。
2. **升级定位受既有验证结论约束**：2026-07-03 门控评估中四类缠论信号（三买/三卖/顶背驰/底背驰）
   全部未过 OOS 门控，chanlun 已按 verdict B 定位为"结构位置过滤器"。因此本升级的目标是：
   - ① 提高结构识别保真度——当前实现（无线段、仅末中枢三买三卖、单一背驰度量）过于简化，
     结构噪声本身可能是"无 edge"结论的成因之一；
   - ② 补齐 verdict B 遗留的 `structure_position` 证据包接入（risk_redteam 引用）；
   - ③ 用升级后的结构以**版本化 strategy_id + 新留出集**重跑门控评估。
     过闸与否仍由 `research_gate` 裁决；升级本身不给任何信号加权。
3. **不整库 vendor 进生产路径**：chan.py 重 OOP、全程可变状态、配置解析用 `exec()`（本仓库
   安全红线）、pickle 序列化。做法改为：**chan.py 作为算法规格 + 差分测试 oracle 放
   `third_party/`（仅测试引用），生产侧在现有纯函数体系内重写所需子集**。

## 1. 现状 vs chan.py 能力对照

| 能力 | 现状（chan_structure.py，299 行） | chan.py（约 7.2k 行） | 差距 |
|---|---|---|---|
| K线去包含 | 有（方向法） | 同类，另可处理跳空（gap_as_kl） | 小 |
| 分型 | 三元组极值，无有效性检查 | 4 种分型有效性检查（strict/half/loss/totally） | 中 |
| 笔 | 顶底交替 + 固定 4 根间隔 | 严格/宽松笔、**虚笔 is_sure=False（未确认笔）**、sub_peak、端点峰值校验 | **大** |
| 线段 | **无** | 特征序列分型（EigenFX，含缺口规则）+ 备选算法；线段的线段 | **大** |
| 中枢 | 滑窗 3 笔重叠，不合并，可跨线段 | 线段内构造、zs/peak 双合并模式、bi_in/bi_out、峰值区间、线段级中枢 | **大** |
| 买卖点 | 仅最后一个中枢的三买/三卖 + 最近两笔背驰 | T1/T1P/T2/T2S/T3A/T3B 全谱系、12 个可调参数、线段级买卖点、relate_bsp1 关联 | **大** |
| 背驰度量 | MACD 柱面积 1 种 | 12 种（area/peak/full_area/diff/slope/amp/量额/RSI…）+ divergence_rate 阈值 | 中 |
| 多级别 | four_dim_scorer 各自跑日线/60m，无联立 | 父子 K 线对齐 + 一致性校验 + 区间套 | 中→大 |
| point-in-time | 全量重算，回测靠前缀重放（O(n²)） | 增量更新 + is_sure 确定态 + trigger_step 逐 K 回放 | 中（性能/语义） |
| 信号特征 | detail 字符串 | 每个买卖点带 feature_dict（divergence_rate、retrace_rate、amp…） | 中 |

**明确不学**：`exec()` 动态配置（安全红线）、pickle 状态持久化、全局可变链表风格、print 告警。

## 2. 萃取清单（按价值排序）

1. **虚笔/确定笔（is_sure）语义**——"信号首次可观察时点"成为模块内建属性，回测与实盘同一语义
   （当前由 chan_signal_backtest 前缀重放外部保证，慢且脆）。
2. **线段（特征序列分型 + 缺口规则）**——三类买卖点的正宗定义建立在线段与段内中枢上；
   当前用笔中枢近似是最大结构性偏差。
3. **段内中枢 + 中枢合并 + bi_in/bi_out**——背驰的进/出笔标准化。
4. **全谱系买卖点 + feature_dict**——尤其 T1（趋势背驰）/T1P（盘整背驰）/T2S（类二）；
   特征直接供 four_dim_scorer / 未来 ML。
5. **背驰算法族**（至少 area/peak/slope）+ divergence_rate 可调。
6. **多级别父子对齐 + 区间套**（日线确认 + 60m 定位）。
7. **K 线数据一致性检查**（misalign/inconsistent 容错阈值，fail-closed 精神与本仓库一致）。

## 3. 落地架构

- 生产路径仍为 `skills/chanlun-backtest/scripts/` 纯函数模块，按 coding-style（≤400 行/文件）拆分：
  - `chan_kline.py`：去包含 + 分型（含有效性检查）+ 笔（含虚笔）——从 chan_structure.py 拆出并升级
  - `chan_segment.py`：特征序列线段（T2 实际落地时拆为两个文件：`chan_eigen.py` 特征序列 +
    特征序列分型，`chan_segment.py` 线段列表/左侧收尾/对外契约；单文件会到 ~470 行，超出
    ≤400 行的可维护区间。对外入口仍只有 `chan_segment.py`）
  - `chan_center.py`：中枢（段内构造、合并、bi_in/bi_out）
  - `chan_bsp.py`：买卖点全谱系 + 背驰算法族
  - `chan_structure.py`：保留 `analyze()` 门面，**输出契约向后兼容**（只增字段不删旧字段），
    four_dim_scorer / chan_signal_backtest / chanlun_gate_evaluation 不改即可运行
- `third_party/chan_py_reference/`：chan.py pinned `429d6ed` 快照 + LICENSE + 出处 README；
  **仅测试引用**，生产 import 由守卫测试禁止。
- 差分测试：同一组 K 线灌两边，断言笔端点/线段端点/中枢区间/买卖点位置对齐率
  （规则差异需白名单化并写明原因）。

## 4. 任务拆分与模型分配

| # | 任务 | 依赖 | 模型（dispatch.md 依据） | 验收标准（机械可查） |
|---|---|---|---|---|
| T0 | vendor chan.py 到 third_party + 生产 import 守卫测试 + 参考侧可离线跑通的最小 driver | — | sonnet（单发、模式明确） | 守卫测试红→绿演示；driver 在合成 K 线上输出笔列表 |
| T1 | 笔+分型升级（虚笔 is_sure、fx_check、严格笔），拆出 chan_kline.py | T0 | opus（长程 agentic 实现，首发即 opus） | 合成 K 线单测全绿；与参考实现笔端点差分对齐率 ≥95%（差异白名单化）；analyze() 旧契约回归不破 |
| T2 | 线段 chan_segment.py（EigenFX + 缺口规则） | T1 | opus | 线段端点差分对齐 ≥90%；单测覆盖缺口/未确认段 |
| T3 | 中枢升级 chan_center.py（段内构造/合并/bi_in_out） | T2 | opus | 中枢区间差分对齐 ≥90%；跨线段中枢用例消失 |
| T4 | 买卖点全谱系 chan_bsp.py + 背驰算法族 + feature_dict | T3 | opus | 六类买卖点在参考实现产出的相同位置触发（白名单差异除外）；每信号带 feature_dict |
| T5 | 多级别区间套 + structure_position 证据包接入 four_dim_scorer/risk_redteam（verdict B 遗留项） | T4 | sonnet | 证据包 schema 测试；未过闸信号仍 0 权重的回归测试 |
| T6 | 门控重评：chanlun_gate_evaluation 用 `_v2` strategy_id + 新 split 重跑 | T5 | sonnet（runner 已存在） | 产出新 gate_evaluation 报告；无论 A/B 结论都落盘文档 |

- 执行纪律：**串行派发（同时至多 1 个写型子代理）**；每个 T 完成后由 fresh-context
  code-reviewer（sonnet）按验收清单读回 + 实跑测试（dispatch.md §5，不自评）。
- 分支策略：`feat/chanlun-upgrade-t<N>` 每任务一分支一 PR，本地四步门禁
  （pytest / cron manifest / ruff / maintainability）全绿才算完成；**不 push 由主会话把关后执行**。
- 铁律不变：所有新信号在 `research_gate --register` 通过前 display-only / 0 权重。

## 5. 风险与边界

- **最大风险**：结构保真度提高 ≠ 出现 edge。T6 完全可能再次给出 verdict B——那也是有效结论
  （省下后续信号化投入，结构证据仍服务于 risk_redteam）。
- 差分对齐率阈值（95%/90%）是首轮假设值，允许在白名单机制下调整，但每条差异必须写明规则出处。
- chan.py 的多级别数据 API（BaoStock 等）不引入；数据仍走本仓库 mootdx/腾讯源。
- 不做的部分：Plot/GUI、Demark 等附加指标、ML/automl 框架对接（chan.py 未开源该部分）。
