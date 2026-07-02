# Dual-Runtime Audit Report 2026-07-02

## Problem

`architecture-hardening.md` already documents the theoretical risk: the
run-lease mutex in `skills/common/run_lease.py` only prevents Hermes and
OpenClaw from double-running the same DAG node if both point at the same
physical `A_STOCK_STATE_HOME`. Two machines with a same-named but distinct
local directory get zero real mutual exclusion, and nothing before this
change actually checked for that in production.

## Change

Added `scripts/dual_runtime_audit.py`, a read-only diagnostic (no lease
claims, no job execution, no writes) that reports:

- `runtime_distribution`: how many recorded runs came from each `runtime`
  value in `job_runs.json`.
- `concurrent_duplicate_runs`: `(job_id, trading_date, batch_id)` groups
  completed (`status=ok`) by more than one runtime within a configurable
  window (default 300s) — direct evidence of the same node actually
  executing twice.
- `active_leases`: any `runtime/leases/**/*.lease` directory currently held,
  with age — leftover/stuck leases past their TTL are a sign of crash
  cleanup gaps, not just double-execution.
- `state_identity`: this machine's `state_identity.json` (state_id +
  initial_root), meant to be diffed against the same file on the other
  runtime's host.
- `openclaw_registration`: if the `openclaw` binary is present, cross-checks
  `openclaw cron list` against the manifest's enabled jobs for
  missing/orphaned registrations.

Run it on every machine that might execute cron jobs and compare:

```bash
python scripts/dual_runtime_audit.py
```

## Finding on this machine (2026-07-02)

```json
{
  "runtime_distribution": {"claude-code": 882, "hermes": 4},
  "concurrent_duplicate_runs": [],
  "active_leases": [],
  "openclaw_registration": {"status": "unavailable", "reason": "openclaw binary not found on this machine"}
}
```

No `openclaw`-tagged runs exist in `job_runs.json` on this machine, the
`openclaw` binary isn't installed here, and there's no local crontab. This
machine currently only runs jobs via Claude Code sessions and the `hermes`
runtime tag — no evidence of double execution *because there is no second
runtime active here to double with*.

**This does not clear OpenClaw.** If OpenClaw runs on a separate host, this
script needs to run there too — that machine is the one the original plan's
manual checkpoint refers to. Please run
`python scripts/dual_runtime_audit.py` on the OpenClaw host and compare its
`state_identity.initial_root` against this machine's
(`/Users/na/.a-stock-agent-cc`); if they differ, the run-lease mutex is not
providing real protection between the two runtimes regardless of what
`runtime_distribution` shows on either side individually.

## Test plan

- `pytest` full suite: 832 passed
- New tests: `tests/test_dual_runtime_audit.py` (duplicate-run detection,
  window boundaries, held-lease parsing, openclaw registration diff, clean
  report shape, CLI smoke test)
- Manually ran the script against the real `/Users/na/.a-stock-agent-cc`
  state (see finding above)
