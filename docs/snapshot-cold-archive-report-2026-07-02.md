# Snapshot Cold Archive Report 2026-07-02

## Problem

`snapshot_gc.py` (via `skills/common/storage_retention.py`) permanently
deletes market snapshots once they age past `snapshot_input_retention_days`
(30) / `snapshot_output_retention_days` (90) or the dataset exceeds
`snapshot_max_total_mb`. That's the only historical, point-in-time record of
what the scoring pipeline actually saw on a given day — the same data T5
(weight calibration) would need to regress `config/scoring.yaml`'s
30/15/30/25 weights against T+3 outcomes. Deleting it outright means that
research becomes permanently impossible for anything older than the hot
retention window, regardless of how much settled outcome data eventually
accumulates in `signal_ledger.jsonl`.

## Change

`storage_retention.py::cleanup_storage()` now gzip-archives a snapshot into
a cold tier immediately before deleting it, instead of just deleting it:

- New config key `storage.snapshot_cold_archive_enabled` (default `true`,
  `config/data_access.json` + `data_access_config.py` DEFAULTS/normalization).
- New `archive_dir(home)` → `{state_home}/archive/snapshots/` — outside the
  git repo by construction (it's under `A_STOCK_STATE_HOME`, same as every
  other runtime state path), so no `.gitignore` change was needed.
- `_archive_file()` mirrors the snapshot's relative path under the archive
  root and gzip-compresses it (`*.json` → `*.json.gz`). Best-effort: an
  archive write failure (disk full, permissions) does not block the
  snapshot's deletion — same fail-soft posture as the existing
  invalid-snapshot handling.
- Scope is snapshots only, not cron artifacts — cron artifact deletion is
  unchanged. `snapshot.py`'s write path is untouched, as scoped.
- The archive tier has **no automatic retention/cap in this change** — it
  intentionally trades bounded hot storage for unbounded point-in-time
  research accumulation, which was the stated goal. A future PR can add a
  separate (much longer) archive retention policy once real growth data
  exists; adding one now would be guessing at a number with no data behind
  it.
- `cleanup_storage()`'s report gained an `"archived"` section
  (`enabled`, `archive_root`, `count`, `bytes` — `bytes` is `null` in
  dry-run mode since compression ratio isn't known without doing it).

## Real-state dry run (2026-07-02)

```json
{
  "mode": "dry_run",
  "scanned": {"snapshots": 365, "cron_artifacts": 0, "reference_files": 940, "invalid_snapshots": 0},
  "deleted": {"expired_snapshots": 0, "size_cap_snapshots": 0, "cron_artifacts": 0},
  "archived": {"enabled": true, "archive_root": "/Users/na/.a-stock-agent-cc/archive/snapshots", "count": 0, "bytes": null},
  "capacity_satisfied": true
}
```

Nothing is expired yet on this deployment (state root is ~1 week old, well
inside the 30/90 day windows), so there's nothing to archive today — this
change is purely forward-looking: the first time `snapshot-gc` actually
deletes something, it will land in the cold tier first instead of vanishing.

## Test plan

- `pytest` full suite: 826 passed
- New/updated tests in `tests/test_storage_retention.py`: archive-before-delete
  with lossless gzip round-trip verification, dry-run reports without
  writing, config-disabled skip path, cron artifacts are never archived
- `tests/test_data_access_config.py`: invalid-value fallback covers the new
  boolean key
- `python scripts/smoke_test.py` → 11/11 passed
- Manual dry run against real production state (above)
