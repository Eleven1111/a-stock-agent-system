# Adaptive Schedule Report 2026-07-02

## Problem

Several high-frequency cron jobs poll at a fixed wall-clock cadence
regardless of whether they're producing anything: `official-policy-watch`
scans every 10 minutes from 08:00-22:00 on *every calendar day including
weekends* (`trading_day_policy: calendar_day`), and the two news pipelines
(`news-monitor`, `news-monitor-intraday`) run at full frequency even when
nothing new has appeared for hours. None of this backs off, and none of it
bursts back up faster when something is actually happening — cadence never
follows the actual information output rate.

## Change

- `skills/common/adaptive_schedule.py` (new): a per-job miss-streak counter
  persisted at `{state_home}/runtime/adaptive_schedule.json`.
  - `should_run(job_id)` advances the tick counter and decides whether this
    tick is "due" against a backoff table: streak 0-2 → every tick, 3-8 →
    every 2nd, 9-20 → every 4th, 21+ → every 8th (caps there).
  - `record_outcome(job_id, ran, has_signal)` updates the streak after a
    tick resolves: `has_signal=True` resets the streak to 0 immediately —
    the escape hatch the plan's own discipline requires. A tick that was
    actually skipped (`ran=False`) leaves the streak untouched, since no
    real observation happened.
  - Found and fixed a real bug while writing tests for this: the module's
    first draft used a shared mutable dict as the `mutate_json` default
    argument, so job state leaked across unrelated job_ids within the same
    process (caught by cross-test contamination in
    `tests/test_adaptive_schedule.py`, not by inspection).
- New `adaptive_backoff` section in `config/delivery_policy.json`
  (`enabled: true, mode: shadow`), reusing the existing
  `delivery_policy.py` enabled/mode/shadow/enforce helpers from T2 rather
  than re-inventing them.
- `hermes_job_runner.py::run_job()`: for jobs with `"adaptive_backoff": true`
  in the manifest, calls `should_run()` right after the dependency gate
  passes. In `enforce` mode, a tick that isn't due writes a
  `status=skipped_adaptive_backoff` artifact and returns without running
  the business command at all (real cost savings: no provider fetch, no
  push). In `shadow` mode (the shipped default), the job always actually
  runs regardless of the decision — only the decision itself is recorded
  on the artifact (`adaptive_schedule` field) for later inspection. No
  change was needed in `run_agent_dag.py`'s external-facing
  `target_output()`: a skip artifact has empty stdout, `has_signal=False`
  falls out of the existing no-signal detection, and all three target jobs
  already have `silent_when_no_signal=true`, so it's suppressed the same
  way a genuine empty run already is.
- Manifest: `official-policy-watch`, `news-monitor`, `news-monitor-intraday`
  opt in via `"adaptive_backoff": true`. `validate_cron_manifest.py` checks
  the field is boolean when present (optional field, same pattern as
  `silent_when_no_signal`).

## Why shadow-first here (unlike T2)

T2's dedup change trades an occasional duplicate push for simplicity; the
worst case is redundant noise. Here the worst case is silently missing a
real policy announcement or news event during a backed-off window — a
different risk class. The plan's own discipline explicitly calls for a
shadow period + has_signal escape hatch for exactly this kind of
frequency-reduction change, so this ships with `mode: shadow` by default:
every tick still executes for real, and `adaptive_schedule.json` plus each
artifact's `adaptive_schedule` field accumulate enough history to review
before anyone flips it to `enforce`.

## Test plan

- `pytest` full suite: 832 passed
- `tests/test_adaptive_schedule.py`: backoff progression, escape-hatch
  reset on real signal, skipped ticks don't extend the streak, per-job
  isolation, unresolved (`has_signal=None`) outcomes are ignored
- `tests/test_hermes_job_runner.py`: enforce-mode actually skips execution
  and writes the skip artifact; shadow-mode (real shipped default) still
  runs the job even when the policy would skip it; a successful run
  updates the persisted streak correctly
- `tests/test_cron_manifest.py`: the three target jobs carry
  `adaptive_backoff: true`
- `python scripts/validate_cron_manifest.py` → OK: 39 jobs
- `python scripts/smoke_test.py` → 11/11 passed
