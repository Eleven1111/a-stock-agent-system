# P2 remediation and release-gate record

Date: 2026-07-10
Target version: 1.4.0
Status: P2 code controls implemented and locally verified; empirical validation
and production release remain blocked.

## What this change completes

- Two-invocation, append-only OOS precommit/result registration binds the Git
  ancestor, clean tree, rules, dataset, split, thresholds, variants and folds.
- Walk-forward folds apply purge/embargo boundaries. Empty, failed and
  `not_evaluated` folds remain part of the immutable result.
- The repository calculates moving-block bootstrap, Newey-West HAC,
  Benjamini-Hochberg FDR, PBO and deflated Sharpe from bound return series.
- Independent sample breadth is calculated from trade-session/stock,
  stock and market-regime clusters; callers cannot attest their own sample
  count.
- Complete validation reports retain every precommitted variant and calculate
  turnover, maximum drawdown, lower-tail loss, cost stress and capacity curves.
- Daily validation evidence is accepted only as a valid PIT market snapshot,
  copied to a content-addressed store, and removed from coverage if that stored
  artifact is missing or corrupted.
- Point-in-time evidence uses versioned stage cutoffs, publication delays and
  provider replay capability. A current-only provider cannot fill historical
  replay with live data.
- K-line provenance records provider, version, adjustment, fetch time and
  event-asof. Stale, corrupt or incompatible cache entries degrade explicitly.
  Historical qfq replay is blocked unless point-in-time adjustment factors can
  be proven; current adjustment factors are never treated as historical facts.
- Plain-HTTP Tencent quotes remain non-directional. Caller-supplied mappings
  cannot self-attest as authenticated corroboration; promotion requires a
  future adapter-bound evidence chain rather than trusted-looking fields.
- Execution output separates signal, conditional-fill and conservative
  unfilled scenarios, includes versioned fees/taxes, and marks unknown capacity
  or corporate actions as estimate-only/reconciliation-required.
- Board-aware price-limit resolution keeps ChiNext and STAR risk-warning shares
  at 20% while main-board ST shares use 5%, following the
  [SZSE ChiNext rule notice](https://www.szse.cn/disclosure/notice/general/t20200710_579459.html)
  and [SSE STAR clarification](https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20240412_10753148.shtml).
- Portfolio admission checks fresh, versioned correlation, beta, style, ADV
  participation and portfolio-volatility evidence. Missing evidence blocks new
  risk.
- Strategy promotion is an ordered research/shadow/manual-pilot/live state
  machine with versioned thresholds, zero shadow ranking effect, explicit
  approval, bounded pilot weight and automatic demotion.
- Research-agent findings resolve citations inside a content-addressed evidence
  pack and carry model, prompt, input and tool hashes. External text is marked
  untrusted data. Unreviewed findings remain `review_only`; reviewed manifests
  are revalidated again at synthesis time.
- Cash, position/trade and monitor mutations append canonical events before
  projections. Projection checkpoints advance only after every projection
  succeeds; restart replay repairs interrupted projections, and reconciliation
  mismatch blocks new risk. The monitor ledger is now only a compatibility
  mirror of the canonical signal ledger.
- Settlement reports distinguish aged pending and terminal-unresolved records,
  expose a terminal coverage ratio, and prevent strategy gating when coverage
  is insufficient.
- A repository-wide maintainability baseline prevents growth in `sys.path`
  mutations, broad exception handlers and oversized functions. Modified legacy
  debt requires a time-limited waiver.
- Public project governance now includes MIT licensing, security and
  contribution policies, CODEOWNERS, constrained direct dependencies, pinned
  GitHub Actions and Python 3.10/3.13 checks.

## What is deliberately not claimed

The code cannot create historical observations that did not occur. These
release gates remain external and fail closed:

1. at least 60 real, calendar-verified A-share trading days of immutable PIT
   evidence;
2. the precommitted shadow duration with simulation error inside threshold;
3. normalized broker statements reconciled against cash, positions and trades;
4. all statistical, execution-cost and capacity gates passing on that real
   evidence;
5. owner-confirmed rotation/revocation of any credential that may have appeared
   in public Git history, followed by separately authorized history cleanup.

Until all five are satisfied, `production_release=blocked`. Issue #32 remains
the tracking surface for the empirical OOS requirement.

## Migration and operations

1. Install with `python -m pip install -c constraints.txt -e ".[dev]"`.
2. Ensure Hermes and OpenClaw share `A_STOCK_STATE_HOME`; local files do not
   provide cross-machine exclusion.
   Historical strategy-registry records without a promotion state are treated
   as `research_only` and must be re-enrolled; they retain no legacy live
   weight.
3. Set `A_STOCK_MODEL_VERSION` or pass `--model` when submitting a directional
   research finding so its run manifest is auditable.
4. Start a validation precommit from a clean committed tree. Reveal results in
   a later process invocation; same-invocation reveal is rejected.
5. Do not bypass `historical_replay_unsupported`, `rule_unknown`,
   `statistics_not_evaluated`, `capacity_unknown` or reconciliation blockers.

## Verification commands

```bash
pytest -q
python -m ruff check .
python scripts/validate_cron_manifest.py
python -m compileall -q scripts skills tests
node --check skills/a-stock-daily-report/scripts/a-stock-report.js
python scripts/check_maintainability_budget.py --base-ref origin/main
git diff --check
```
