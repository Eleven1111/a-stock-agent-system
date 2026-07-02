# Delivery Dedup Report 2026-07-02

## Manifest Push Window Comparison

Baseline is `HEAD:cron/hermes-cron-manifest.json` on branch creation. After is this branch.

| Window / job group | Before | After | Fixed effect |
| --- | ---: | ---: | --- |
| Enabled `deliver=origin` jobs | 17 | 15 | -2 origin push jobs |
| 09:26 auction-finalize + 09:27 auction-intelligence-brief | 2 origin pushes | 1 origin push | `auction-finalize` now `deliver=local`; 09:27 brief remains origin |
| 09:35 open-confirmation + 09:36 open-intelligence-brief | 2 origin pushes | 1 origin push | `open-confirmation` now `deliver=local`; 09:36 brief remains origin |
| news-monitor / news-monitor-intraday / catalyst-trigger / official-policy-watch | All origin, independent duplicate handling | All origin, shared 7-day novelty cache | Exact duplicate content keys are archived as counts; all-duplicate runs become no-signal |
| global-preopen | Full JSON on no anomaly | Origin push, no-anomaly summary mode | <=200 char JSON summary unless anomalies exist |
| capital-flow | Full JSON on no anomaly | Origin push, no-anomaly summary mode | <=200 char JSON summary unless anomalies exist |
| closing-triage | Full context digest on no missing context | Origin push, no-anomaly summary mode | <=200 char line unless missing context exists |
| portfolio-check | Full output | Full output | Unchanged by design |

## Delivery Policy

`config/delivery_policy.json` defaults both `novelty_gate` and `summary_mode` to `enabled=true`, `mode=enforce`.

Switching either mode to `shadow` keeps the full output but appends `would_suppress=true` telemetry rows to `$A_STOCK_STATE_HOME/cron/push_telemetry.jsonl`.

## Runtime Safety Notes

- The novelty gate fails open on unreadable or corrupt cache state.
- The gate suppresses only exact normalized content-key duplicates across the four information pipelines.
- Existing `has_signal` logic and intraday alert thresholds are unchanged.
- Summary mode is output-only; collection, cache writes, and anomaly detection remain unchanged.
