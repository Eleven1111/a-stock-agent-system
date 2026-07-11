# P0 审查整改记录

> 日期：2026-07-10
> 分支：`codex/p0-audit-remediation`
> 基线：`8e59b53a96ffad541396e20e42a8081a502ecbe1`
> 来源：`PROJECT_AUDIT_REPORT_2026-07-10.md`

## 结论

本轮已完成全部可在仓库代码中直接修复的 P0，并为两个不能即时完成的治理项恢复了 fail-closed 台账。P0 尚不能整体关闭：GitHub Actions/分支保护受账户 billing 和私有仓库套餐能力阻断；组合级盈利能力仍需积累至少 60 个真实 point-in-time 交易日，不能用代码或测试替代。

## 状态

| 审查项 | 状态 | 整改 |
|---|---|---|
| F-01 组合级 OOS 台账提前关闭 | 进行中 | GitHub Issue #32 已重新打开，9 项验收保持未完成；新增严格关闭说明 |
| F-02 盘中信号用信号日收盘结算 | 已修复 | 只使用信号发生时记录的可观察价格；缺价/畸形价保持 pending |
| F-03 涨停开盘用日内 low 反推成交 | 已修复 | 开盘触及涨停一律保守拒绝，不再使用全天信息判断开盘成交 |
| F-04 四维评分/日报方向性旁路 | 已修复 | 原始评分只输出研究候选，`signals=[]`、`execution_action=none`；日报取消固定仓位和伪资金流描述 |
| F-05 持仓行情缺失误报正常 | 已修复 | `valuation_unknown`、总资产/权重未知、阻断新增风险，保留值只作 stale 参考 |
| F-06 极弱市场救回候选 | 已修复 | 删除 `research_only -> deliverable_watch` 覆盖；空可交付候选合法且不注册监控 |
| F-07 CI/主分支门禁不存在 | 外部阻断 | 新建 GitHub Issue #94；不得为获得 ruleset 直接公开含历史敏感内容的仓库 |

## 行为变化

### 绩效结算

- 结算入场价按 `signal_price`、兼容的 entry/reference/recommendation 字段读取；
- 不再回退到信号日收盘；
- 缺失、布尔、NaN、Inf、零或负值均不结算；
- settlement event 记录入场价和来源；
- 信号日收盘只保留给 T+1 涨停晋级判断。

### 回测成交

- 前收 10 元、次日 11 元涨停开盘时，无论盘中是否开板，都不假设开盘订单成交；
- 一字板使用 `entry_limit_up_sealed`；盘中开板使用 `entry_limit_up_open`；
- 普通开盘入场、T+1 和跌停延迟退出保持不变。

### 方向性输出

- 四维评分结果显式携带 `directional_ready=false`、`execution_action=none`、`policy_status=not_evaluated`；
- S/A 只进入 `research_candidates`，不能进入批量 cron 的 `signals`；
- 短线条件改为 observation，不再出现“立即执行/可入场”；
- ATR 只展示波动率，不生成止损/目标执行价；
- 日报不再根据涨跌幅声称资金流入/流出，不再固定建议 6–7 成仓位。

### 组合估值与弱市

- 任一持仓行情缺失，组合精确总市值、总资产和全部仓位权重均为未知；
- 新增仓位/加仓返回 `VALUATION_UNKNOWN`，卖出不受阻；
- 行情恢复并刷新后自动解除阻断；
- 极弱市场的 research-only 标的不会为填充报告而进入可交付候选或监控列表。

## GitHub 治理动作

- [Issue #32：组合级 OOS 验收](https://github.com/Eleven1111/a-stock-agent-system/issues/32) 已重新打开；
- [Issue #94：恢复有效 CI 与 main 合并门禁](https://github.com/Eleven1111/a-stock-agent-system/issues/94) 已创建；
- 最新 Actions 仍为 `startup_failure`；
- `main protected=false`；ruleset API 返回私有仓库需升级 GitHub Pro 或公开仓库；
- 由于历史敏感内容尚未清理，公开仓库不是可接受的临时修复。

## 验证证据

### Check: 全量回归

**Command run:**

```text
pytest -q
```

**Output observed:**

```text
1409 passed in 30.37s
```

**Result: PASS**

### Check: P0 非 happy-path 对抗测试

**Command run:**

```text
pytest -q \
  tests/test_performance_tracker.py::test_update_outcomes_uses_observable_signal_price_not_signal_day_close \
  tests/test_performance_tracker.py::test_update_outcomes_keeps_signal_pending_without_observable_entry_price \
  tests/test_performance_tracker.py::test_observable_entry_price_rejects_malformed_values_and_supports_legacy_key \
  tests/test_portfolio_manager.py::test_missing_quote_blocks_new_risk_and_marks_portfolio_valuation_unknown \
  tests/test_portfolio_manager.py::test_one_missing_quote_makes_all_portfolio_weights_unknown \
  tests/test_candidate_discovery.py::test_extreme_weak_market_keeps_research_only_candidates_out_of_live_targets \
  tests/test_portfolio_backtest.py::test_limit_up_open_is_not_filled_using_later_intraday_low \
  tests/test_batch_four_dim.py::test_high_grade_scores_remain_research_only_without_policy_decision \
  tests/test_four_dim_weighting_policy.py::test_short_term_score_never_emits_execution_instruction \
  tests/test_daily_report_policy.py::test_daily_report_does_not_describe_price_rank_as_fund_flow
```

**Output observed:**

```text
.......... [100%]
10 passed in 0.20s
```

**Result: PASS**

### Check: 静态、配置、调度与语法

**Command run:**

```text
python -m ruff check .
python scripts/validate_cron_manifest.py
python -m compileall -q scripts skills tests
node --check skills/a-stock-daily-report/scripts/a-stock-report.js
git diff --check
```

**Output observed:**

```text
All checks passed!
OK: 44 jobs (0 local, 44 external)
# compileall/node/diff-check exit 0，无错误输出
```

**Result: PASS**

### Check: 变更行覆盖率

**Command run:**

```text
python -m coverage run --branch -m pytest -q <117 个相关测试>
python -m coverage json -o /tmp/a-stock-p0-coverage.json
# 将 git diff 新增可执行行与 coverage executed/missing lines 取交集
```

**Output observed:**

```text
117 passed in 4.40s
TOTAL_CHANGED_LINE_COVERAGE=242/261 (92.7%)
```

**Result: PASS**

## 剩余限制

1. CI 和 main 保护需要仓库所有者解决 billing/套餐并配置 required checks；代码无法替代该权限动作。
2. Issue #32 至少需要 60 个真实交易日，当前不能关闭，也不能宣称完整系统已验证盈利。
3. 盘中信号的指数基准仍沿用信号日收盘口径；本轮只修复个股结算前视，alpha 基准口径列入后续 P1。
4. 本轮没有验证另一台 OpenClaw/Hermes 部署机，代码合并后仍需按审查报告执行实机验收。

VERDICT: PARTIAL
