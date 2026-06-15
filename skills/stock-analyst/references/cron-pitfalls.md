# Scheduled Analysis Failure Modes

The canonical schedule is `config/cron_jobs.yaml`. Scheduled analysis must
enter through `scripts/run_agent_dag.py`; historical direct shell wrappers and
conversation-bound prompts are unsupported.

## Context Contamination

Symptom: a scheduled result refers to unrelated conversation state.

Controls:

- Execute business work in an isolated child process.
- Read only declared dependency artifacts and runtime state.
- Never reconstruct holdings or subscriptions from chat memory.
- Emit bounded JSON artifacts before any natural-language summary.

## Duplicate Execution

Symptom: Hermes and OpenClaw process the same run or research request.

Controls:

- Share `A_STOCK_STATE_HOME`.
- Use run IDs, idempotency keys, and leases.
- For multiple machines, use a distributed-capable state store or shared
  filesystem with verified lock semantics.
- Do not treat a host-local lock as cross-machine exclusion.

## Stale Inputs

Symptom: an earlier trading day's ladder, auction, or quote affects a current
decision.

Controls:

- Validate `trading_date`, `batch_id`, producer version, and source timestamp.
- Reject future-dated and expired inputs.
- Fail closed when the trading calendar does not cover the requested year.
- Required dependency failure produces `status=blocked`.

## Provider Failure

Symptom: empty or malformed provider data appears as “no risk”.

Controls:

- Use shared HTTP/provider adapters with schema validation, retry limits,
  rate limiting, and circuit breaking.
- Keep the last trustworthy snapshot only within its declared freshness
  window.
- Distinguish `complete`, `partial`, `blocked`, and unavailable datasets.
- Never fabricate news or quotes after an API failure.

## Schedule Drift

Symptom: documentation and deployed jobs disagree.

Controls:

- Do not duplicate exact schedules in Skill docs.
- Validate the manifest after every schedule change.
- Keep high-frequency no-signal tasks silent.
- Avoid overlapping jobs by dependency design and leases, not by relying only
  on hand-spaced clock times.

## Verification

```bash
python scripts/validate_cron_manifest.py
pytest -q tests/test_cron_manifest.py tests/test_agent_job_runner.py
git diff --check
```
