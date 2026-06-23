# Portfolio Research Protocol

This protocol answers one question: does the complete recommendation policy make
money after A-share execution constraints, rather than whether one event factor
has an attractive average return?

## Evidence boundary

The replay accepts only `portfolio_backtest_input_v1` files assembled from
candidate snapshots that were persisted at decision time. Do not reconstruct an
old candidate list with current news, current fundamentals, current security
names, or a scorer changed after the split date.

Every snapshot must contain:

- `date`, `generated_at`, and non-empty `source_versions`;
- candidates with `code`, final production `score`, `evidence_asof`, and optional
  component scores used for ablation;
- the final policy decision when available. `avoid`, `watch`, rejected quality,
  and `eligible=false` candidates cannot become trades during replay.

The input also contains historical OHLCV by code and benchmark OHLCV. The CLI
binds the exact input file, rules, results, controls, and gate metrics into a
SHA-256 research artifact.

## Automatic evidence collection

The live `open-confirmation` job writes one immutable research snapshot per
trading date under:

```text
$A_STOCK_STATE_HOME/skills/stock-triage/data/portfolio_research_snapshots/
```

Only a run generated on its actual trading date may create this evidence.
Historical reruns are skipped because they have already observed future
information. Repeating the same live result is idempotent; replacing that day's
snapshot with different content fails closed.

Each candidate preserves `selection_context`: market-timing status,
mainline-sector rank and persistence, within-sector leader rank, confirmation
window, and the immutable selection snapshot reference. Portfolio OOS reports
can therefore test whether timing/sector/leader gates add incremental value
instead of attributing all performance to the combined score.

An unregistered strategy remains `watch` in the live recommendation, but its
pre-admission `buy` intent is retained in the research snapshot only when the
sole blocking reason is `strategy_unverified`. Announcement, market-regime,
portfolio-risk, and data-quality blocks are never removed for research replay.

After outcome OHLCV has accumulated, assemble the executable input without
reconstructing candidate lists:

```bash
python scripts/build_portfolio_research_input.py \
  --market-data portfolio_outcome_bars.json \
  --rules-locked-at 2026-06-21T09:34:00+08:00 \
  --start 2026-06-21 \
  --end 2026-09-30 \
  --output portfolio_backtest_input.json
```

`portfolio_outcome_bars.json` contains `bars_by_code` and `benchmark_bars`.
Outcome bars may be collected later; candidate membership, scores, policy state,
source versions, and evidence time must come from the immutable daily snapshots.

## Execution model

- Default entry: the next trading session open after the persisted snapshot.
- A-share T+1: exit is no earlier than the close of the following trading
  session after entry.
- Orders use 100-share lots, available cash, position limits, commission, stamp
  tax, and two-sided slippage.
- Missing bars, zero volume, sealed one-price limit-up entries, future-dated
  evidence, duplicate open positions, incomplete horizons, and policy-rejected
  candidates fail closed.
- The production ranking, controls, ablations, and counterfactuals use one fixed
  evaluation horizon derived from the same snapshot split. Cash-only dates stay
  in the curve so a delayed or sparse variant cannot improve its comparison by
  silently shortening the benchmark period. IS evaluation stops before the OOS
  split.

## Required comparisons

The report contains:

- the exact production ranking;
- benchmark buy-and-hold;
- deterministic random ranking;
- an equal-weight candidate ranking control;
- one full replay per disabled score component.
- a rank-shift proxy and a one-session entry-delay proxy on the same OOS dates;
- a no-action baseline whose excess return is the negative benchmark return.

Ablation re-ranks candidates and replays trades. It does not subtract a component
from already observed returns.

The delay proxy does not observe a new confirmation signal and must not be
described as "confirmation then buy". With `top_n > 1`, the rank-shift proxy is a
shifted basket rather than one runner-up stock. These limitations are emitted in
`counterfactual_contracts`.

Runtime capital is read from the shared `portfolio.json`, not from repository
configuration. Missing account state has zero available capital and fails
closed. Reconcile a verified cash balance with an auditable source and as-of
date before using amount-based guidance:

```bash
python skills/stock-triage/scripts/portfolio_manager.py \
  --reconcile-cash 20000 --cash-source user_confirmed \
  --cash-asof 2026-06-23 --json
```

This updates runtime state only; account balances must never be committed to
the repository.

## Run

```bash
python skills/chanlun-backtest/scripts/portfolio_backtest.py \
  --input portfolio_backtest_input.json \
  --split 2025-01-01 \
  --artifact portfolio_backtest_oos.json \
  --json
```

The default gate requires at least 60 paired OOS trading-day observations. A
positive total return alone is insufficient: the daily excess must be positive,
survive the paired sign-flip test, and remain bound to a valid artifact.

## Factor research migration

`daban_bt_event_table_v1` is no longer valid for formal OOS because it lacks T+1
high, low, and volume fields. Rebuild it as v2 before running:

```bash
python skills/chanlun-backtest/scripts/daban_bt_run.py \
  --build 20240601 20260601 \
  --source mootdx \
  --split 20250601 \
  --oos \
  --artifact daban_gap_oos.json \
  --json
```

The formal H1 test now uses disjoint signal/control groups, entry at the next
session open, exit no earlier than the following session (delayed again by a
sealed limit-down), one equal-weight observation per trading day, and a paired
sign-flip test. `open_close` is retained only as an illegal-in-A-shares intraday
diagnostic; `board_overnight` remains descriptive because daily bars cannot prove
that a limit-up order filled.

Chan walk-forward v2 uses the same T+1 boundary: first observable signal, next
session open entry, then the following session close for T+1 evaluation. Results
generated by `chan-walk-forward-v1` must not be registered as executable evidence.

Chan signal registration also requires persisted artifacts:

```bash
python skills/chanlun-backtest/scripts/chan_signal_backtest.py \
  --input chan_research_dataset.json \
  --split 2025-01-01 \
  --artifact-dir chan-oos-artifacts \
  --register \
  --json
```

## Interpretation

`blocked` means the evidence chain is incomplete. `failed` means the chain is
valid but the strategy did not clear the return/statistical threshold. Only
`passed_for_reference` may enter `strategy_registry`, and it remains subject to
announcement, tradeability, portfolio-risk, and live-performance retirement
gates.

Live admission is fail-closed. A missing registry record, a legacy record with
no evidence artifact, a mismatched strategy ID, a modified artifact/source, or
a disabled live-performance gate produces zero position sizing. Existing
pre-artifact registry entries therefore become research-only until rerun through
the current gate; do not manually edit `strategy_registry.json` to bypass this
migration.
