# Candidate-Lifecycle Analytics: Funnel Recall + Score Calibration (T4/T5)

## The reframe

T4 and T5 were originally scoped against `signal_ledger.jsonl`. That source is
unusable for statistics: 15 `recommendation.created` events, **zero** settled
(`signal.opened` / `signal.t3_settled` are both 0). The original T5 goal —
calibrate the four_dim `30/15/30/25` weights against T+3 outcomes — additionally
requires four_dim sub-scores paired with settled outcomes, and those sub-scores
are not persisted anywhere at scale.

The better data source is `candidate_lifecycle`: the full ~3000-stock discovery
universe per trading day, each record carrying component scores
(`daban_score` / `trend_score` / `leader_score`) plus its raw input features
(momentum, amount, turnover, breakout, …) **and** settled T+1 / T+3 / max_gain
outcomes. That is ~200x more data than the ledger, and it already exists.

All three deliverables below are research-only. None changes a weight or a gate;
per the plan's discipline, calibration produces evidence for human review only.

## T4 — funnel recall & gate regret (`scripts/funnel_recall_report.py`)

Per gate: recall = big movers that survived the gate / big movers that entered;
regret = the outcome distribution of the candidates the gate rejected.

**First-pass finding (1 settled day, 2026-06-25, 3405 records — directional, not
conclusive):** the discovery gate (3000 → top-500) has a **max_gain recall of
0.25** — 466 of the day's big movers were rejected at discovery, and the rejected
pool's mean max_gain (3.17%) was *not* meaningfully worse than the selected pool.
On this day the first gate threw away roughly three-quarters of the winners.

The deeper gates (auction_shortlist → open_confirmed → afternoon_reflow) are
recorded on more recent days (e.g. 2026-06-29) but have not settled yet, so they
appear in the report automatically once their T+3 elapses. The multi-stage funnel
logic is verified by unit tests on synthetic data.

## T5a — discovery-score calibration (`scripts/score_calibration_report.py`)

Spearman IC of each score/feature against settled outcomes, plus quantile-bucket
outcome profiles, plus each raw feature's IC next to its current **hardcoded**
weight in `candidate_pipeline.py`.

**First-pass finding (same single settled day):** the composite `daban_score` has
IC **−0.10** vs T+3 close and **−0.05** vs max_gain — i.e. it ranked stocks the
*wrong* way that day — while individual features it is built from were positively
predictive (`breakout_20d` +0.10, `momentum_5d` +0.095, `change_pct` +0.078 vs
max_gain). A composite scoring worse than its own best components is exactly the
weight-mismatch signal T5 exists to surface. **One day is not a mandate to
retune** — it is a flagged signal that firms up as settled days accumulate.

## T5b — four_dim sub-score instrumentation (`skills/common/four_dim_score_log.py`)

The four_dim sub-scores (`technical` / `sentiment` / `catalyst` / `deep`) that the
`30/15/30/25` weights control are computed daily on the top-N shortlist by
`batch_four_dim_scorer.py`, but were never persisted with an outcome. This adds an
append-only log keyed by `(code, date)` written at scoring time. Because the
scored codes are a subset of the lifecycle universe (which already settles the
whole universe), a later `(code, date)` join yields sub-scores paired with settled
outcomes — closing the gap **without** running four_dim on the full universe or
touching the fragile recommendation/settlement path. The calibration report joins
this log and reports `insufficient_data` until enough paired rows accumulate; the
original four_dim calibration then becomes possible with no further code changes.

## How to run

```bash
python scripts/funnel_recall_report.py --outcome max_gain      # T4
python scripts/score_calibration_report.py                     # T5
```

Both read the live `A_STOCK_STATE_HOME` state and emit JSON. Every report carries
a `research_only: true` flag and a `note` stating the settled-day count and that
no weight is changed.

## Data-quality notes surfaced along the way

- `2026-06-26` lifecycle file is empty (0 records) — a discovery run that failed
  to initialize that day. Worth a separate look; does not block this analysis.
- Only `2026-06-25` is fully settled as of writing (later days' T+3 has not
  elapsed). The reports are built to strengthen automatically as settlement
  accumulates — the value today is the infrastructure plus a flagged preview.

## Test plan

- `pytest` full suite: 873 passed
- New tests: `tests/test_lifecycle_analytics.py` (stage parsing, Spearman IC on
  known ranks, quantile monotonicity, multi-stage funnel recall/regret),
  `tests/test_four_dim_score_log.py` (write/skip-failed/append/fail-open/corrupt),
  `tests/test_calibration_reports.py` (report shapes, insufficient-data paths,
  four_dim join once paired)
- `python scripts/smoke_test.py` → 13/13 (both reports registered)
- Both reports run against the real `/Users/na/.a-stock-agent-cc` state (findings
  above) and against an empty state home (clean `insufficient_data`)
