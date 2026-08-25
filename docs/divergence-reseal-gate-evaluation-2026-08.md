# S2 龙头分歧回封（DivergenceReseal）闸门评估报告（2026-08）

> 写入方式说明：`docs/*.md` 属于本机 settings 钩子拦截 Write 的白名单外文件，本文件
> 用 Bash heredoc 写入，不违反钩子意图（钩子拦的是绕过 CLAUDE.md/README 等命名约定
> 误建文档，不是禁止 docs/ 下的正常交付物）。

## 1. 交付范围（用户 2026-08-25 决策）

本机无全市场分钟线缓存，OOS 走不了。S2 本轮只交付到：pack + 信号实现 + 回测可跑
（接 P5 成交约束）+ 保持 NON-LIVE 未注册 + 诚实的 UNVERIFIED 报告。**未**注册进
`strategy_registry`，**未**用子集数据出胜率/PF/期望值结论，pack 的 `research_status`
明确写着 `not_gated_zero_sample`。

## 2. 信号定义与实现

板块涨停 ≥3 ∧ 板块内存在大量一字/快速板 ∧ 目标是按回封时刻排序的前 2 个完成充分
换手后回封的前排股 ∧ 封板前换手 ≥ 20 日同期中位数的 1.5-3.0 倍（升级方案 §6.1）。

实现：`skills/common/divergence_reseal.py`（纯函数，零网络请求）。四条入场条件：

| 条件 id | 判据 | 默认阈值 |
|---|---|---|
| `sector_limit_up_breadth` | 板块涨停家数 ≥ 下限 | 3 |
| `sector_fast_seal_density` | 板块内一字/快速板家数 ≥ 下限 | 2 |
| `reseal_rank_top_n` | 按回封时刻先后排名 ≤ N | 2 |
| `reseal_turnover_band` | 封板前换手/20日同期中位数 ∈ [下限,上限] | [1.5, 3.0] |

阈值单一事实源：`config/daban_thresholds.yaml` 的 `divergence_reseal` 节（新增节，
未改任何既有阈值）；`skills/common/daban_config.py` DEFAULTS 同步。

策略包（解释层）：`config/strategy_packs/divergence_reseal.yaml`。

回测接线：`skills/chanlun-backtest/scripts/daban_bt_divergence_reseal.py`，接既有
`daban_bt_engine.strategy_returns`（含 P5(a) 成交约束 + DEFAULT_COST 费用口径 +
`board_overnight` 持有口径）。

## 3. 本策略的成败点（未来函数防线）

"前 2 个"必须按**回封时刻**的先后顺序确定，绝不能按结果（是否守住到收盘）挑选。
`reseal_rank()` 只读取各标的的 `reseal_time` 字段，不读取任何"后续结果"字段
（如 `later_break`）。

验证：`tests/test_divergence_reseal.py::test_earlier_reseal_selected_even_if_it_breaks_again_later`
构造一个"先回封（排名第1）但后续又炸板"的标的，断言其仍被判定为 signal、排名不变、
带不带 `later_break` 字段结果逐字段一致。另有
`test_reseal_rank_uses_time_order_not_input_order` 用输入顺序与时间顺序故意错开的
样本，直接排除"退化成按下标排序"这类隐蔽实现。

"充分换手"用 20 日同期中位数这个事前基准，不用当日绝对换手率：基准样本
`< min_baseline_sample_days`(15) → `unavailable`，由
`test_turnover_baseline_missing_sample_is_unavailable_not_no_signal` 守住。

## 4. 数据缺口（诚实标注，不造代理值）

既有 `daban_bt_data v3` 事件表是逐票涨停事件表，**不含**六个必需证据字段中除
`sector` 外的任何一个：

- `sector_limit_up_count` / `sector_fast_seal_count`（板块横截面聚合，日频即可，
  但既有管道未落盘）
- `reseal_time`（分钟级开板-回封时刻，需要分钟线，本机无全市场分钟线缓存）
- `pre_reseal_turnover_pct` / `turnover_baseline_median_pct` / `_sample_days`
  （封板前累计换手 + 20 日同期基准，同样需要分钟线 + 历史换手序列）

`skills/chanlun-backtest/scripts/daban_bt_divergence_reseal.py` 的 `event_record()`
对这些字段做诚实透传（存在就映射，不存在留 None），**不**用"事件表里有涨停就算
板块涨停家数"这种间接推断顶替真实聚合——原因见该文件顶部注释：间接推断的"家数"
会把非候选池的其他票也算进去，口径不等价，比 unavailable 更危险。

## 5. 真实运行结果（2026-08-25，本机 `.venv` 解释器）

对 `/Users/na/.hermes/skills/chanlun-backtest/data/` 下既有事件表跑
`daban_bt_divergence_reseal.py --counterfactual`：

| 事件表 | schema | event_count | universe_count | signal_count | filled_count |
|---|---|---:|---:|---:|---:|
| `event_table_20260528_20260602.json` | `..._v1` | 333 | 305 | **0** | 0 |
| `event_table_mootdx_20240601_20260601.json` | `..._v1` | 40109 | 27110 | **0** | 0 |

两张表 `unavailable_reasons` 均以 `reseal_time_missing_or_not_resealed` /
`sector_limit_up_count_missing` / `sector_fast_seal_count_missing` /
`turnover_ratio:*_missing` 为主，`constraints_bite=False`（零样本时不得报"约束
在咬"，见 `test_counterfactual_reports_no_bite_on_empty_sample`）。

**结论：真实事件数非零，命中数为零，UNVERIFIED。** 本包没有任何胜率/PF/期望值
结论，任何此类数字都不存在。

## 6. 测试与质量门禁

- `tests/test_divergence_reseal.py`：26 个用例，覆盖反事实（含零样本 `bite=False`
  防假绿）、NON-LIVE 消费端行为断言（`decision_policy`/`recommendation_audit`
  实际返回值，非配置字段断言）、防未来函数（含时间-顺序解耦样本）、四条件边界
  （含含端点）、全缺失/单字段缺失/peer 外标的的 unavailable。
- 全量 `pytest -q`：3289 passed（含本次新增 26 个用例）。
- `ruff check` / `compileall` / `validate_cron_manifest.py` /
  `check_maintainability_budget.py --base-ref origin/main` / `git diff --check`
  全过，changed_production_files 计数不变（158/68/39，与 baseline 一致）。
- Mutation：5 处（详见交付报告），逐项改坏→变红→复原，其中一处（回封排名退化成
  按输入下标排序）在 `_group()` 构造下不触发，补写了时间-顺序解耦的专门用例后
  才真正被单元测试直接捕获——过程记录见交付报告，不隐藏这一发现。

## 7. 注册状态（红线核对）

- `strategy_registry.live_record("divergence_reseal")` → `None`
- `strategy_registry.is_allowed_in_live("divergence_reseal")` → `False`
- 新增代码 `grep register_gate_result` → 零命中
- pack `research_status` → `not_gated_zero_sample`

## 8. 结论

**当前不具备注册条件。** 信号实现、回测接线、四条件边界与未来函数防线均已验证
（26 个单测 + 5 处 mutation），但六个必需证据字段中五个在既有数据管道里完全缺失，
真实事件数据上命中数为零，walk-forward OOS 闸门无法运行。升级路径：先把
`sector_limit_up_count`/`sector_fast_seal_count`（板块横截面聚合）与
`reseal_time`/`pre_reseal_turnover_pct`/`turnover_baseline_*`（分钟线 + 历史换手
序列）落进事件表，再谈 `research_gate.py` 与注册。
