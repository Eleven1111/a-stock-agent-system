# P1 审查整改记录 — 第一批

> 日期：2026-07-10
> 分支：`codex/p0-audit-remediation`
> 基线：`8e59b53a96ffad541396e20e42a8081a502ecbe1`
> 状态：主要决策安全项已修复；历史公开暴露和剩余金融研究项未完成

## 已完成

| 审查项 | 整改结果 |
|---|---|
| Agent 深研低分触发强制退出 | 普通或 stale 低分只触发 `review_required`，`action=hold`；价格硬止损仍独立生效 |
| 行业集中度失效 | 新仓必须带行业、来源和分类日期；既有未知行业、分类冲突、未来日期和超过 40% 均 fail closed |
| 市场状态缺失 fail-open | 缺失/异常=`unknown`，过期=`stale`；新方向仓位归零并降为 watch/avoid |
| Signal ledger 静默跳过损坏行 | 非法 UTF-8/JSON/schema/payload 显式抛受控 corruption error，不覆盖原账本或备份 |
| OpenClaw 命令暴露密钥/个人收件人 | 不再序列化 `MIAOXIANG_API_KEY`、飞书用户 ID；origin 收件人必须从显式部署配置读取 |
| Dual-runtime audit 误报 clean | 按 managed logical ID 映射 opaque job；按运行区间检测重叠；任一 inventory/query/registration 失败即 blocked |
| HTTP 全局禁用代理、忽略 Retry-After | 默认 opener 尊重系统代理/NO_PROXY；429/5xx 按有上限的 Retry-After 等待 |
| GitHub 公开仓库基础安全 | Secret scanning 和 push protection 已启用；Dependabot alerts/security updates 与 automated security fixes 已启用 |

## 公开历史安全状态

仓库已确认为 public。GitHub 当前显示 0 条 secret scanning alert，但本地 value-free 历史扫描确认：

- `903d8e2` 仍可从 main 历史到达；
- 该树包含 742 个运行状态文件、826 个 JSON/JSONL artifact、29 个持仓/信号类 artifact；
- 4 个历史 artifact 命中长 `sk-...` 凭证模式。

未读取、打印或写入任何密钥值。对应凭证必须按已泄露处理。详细操作记录位于忽略目录：

`docs_private/public-history-exposure-2026-07-10.md`

需要仓库所有者执行的立即动作：

1. 轮换/撤销相关凭证；
2. 临时改回 private；
3. 备份后使用 `git filter-repo` 清除所有 refs 中的运行时/私密 blob；
4. 强制更新远端后要求部署机和所有 clone 重新同步；
5. 独立全历史 secret scan 通过后再公开。

## GitHub 治理现状

- main 已有 active ruleset，但目前只阻止删除和 non-fast-forward；
- required status checks 仍为空；
- 旧 `startup_failure` run 无法 rerun；workflow 也没有 `workflow_dispatch`；
- 因本轮改动尚未 push/PR，公开化后 CI 是否恢复仍没有远端绿色证据。

## 仍待处理的 P1

1. Point-in-time/as-of 没有贯穿所有 provider、auction replay 和 snapshot 时间断言；
2. 多源 K 线 fallback 的 provider、复权、抓取时间和版本血缘仍不完整；
3. 长期 pending/停牌/退市/数据缺失样本仍可能产生结算 attrition bias；
4. 组合 OOS 的规则锁定、运行次数、FDR 和 bootstrap 仍存在自我声明/统计口径问题；
5. portfolio/cash/ledger/monitor 的跨文件更新仍不是单一事务；
6. Research prompt 的不可信证据封装、artifact 引用校验和模型运行 manifest 尚未完成；
7. 腾讯部分关键行情仍使用明文 HTTP；需要 HTTPS 替代或跨源完整性校验。

## 验证证据

### Check: 全量回归

**Command run:**

```text
pytest -q
```

**Output observed:**

```text
1450 passed in 33.54s
```

覆盖率模式再次执行全量测试：

```text
1450 passed in 43.49s
```

**Result: PASS**

### Check: P1 非 happy-path 对抗探测

**Command run:**

```text
pytest -q \
  tests/test_lhb_patterns.py::test_stale_deep_research_record_is_review_only \
  tests/test_portfolio_manager.py::test_deep_score_below_red_line_creates_review_only_alert \
  tests/test_portfolio_manager.py::test_new_position_requires_classification_provenance \
  tests/test_portfolio_policy.py::test_existing_unknown_sector_makes_concentration_unverifiable \
  tests/test_signal_ledger.py::test_read_fails_closed_on_corrupt_middle_line_without_rewriting_source \
  tests/test_signal_ledger.py::test_backup_sync_fails_closed_when_primary_is_corrupt_and_backup_exists \
  tests/test_openclaw_cron_export.py::test_export_never_embeds_runtime_api_key_value \
  tests/test_market_temperature.py::test_missing_ladder_is_unknown_and_blocks_new_risk \
  tests/test_decision_policy.py::test_unknown_market_context_blocks_positive_action \
  tests/test_dual_runtime_audit.py::test_build_report_is_blocked_when_registration_inventory_is_unavailable
```

**Output observed:**

```text
.......... [100%]
10 passed in 0.20s
```

**Result: PASS**

### Check: 静态、调度和配置

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

### Check: 全量变更行覆盖率

**Command run:**

```text
python -m coverage run --branch -m pytest -q
python -m coverage json -o /tmp/a-stock-p1-coverage.json
# 将 git diff 新增可执行行与 executed/missing lines 取交集
```

**Output observed:**

```text
TOTAL_CHANGED_LINE_COVERAGE=866/906 (95.6%)
FILES_BELOW_80=1
```

低于 80% 的单文件是四维评分的少量格式化分支；本轮 P1 核心模块变更行覆盖率为 81%–100%。

**Result: PASS**

## 判定

代码层第一批 P1 通过验证，但公开历史暴露和上述七项 P1 尚未处理，因此不能把 P1 整体标为完成。

VERDICT: PARTIAL
