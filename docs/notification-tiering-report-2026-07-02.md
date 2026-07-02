# Notification Tiering Report 2026-07-02

## Problem

`deliver=origin` cron output is captured by `run_agent_dag.py::target_output()`
and handed to the Hermes/OpenClaw agent turn, which reads and relays it. For
pure-notification jobs (capital flow, event calendar, policy watch, news
monitor) this content requires no model reasoning, yet it still pays the full
LLM context/token cost on every run.

## Change

Added a new `deliver` policy, `feishu_direct`, that bypasses the agent turn
entirely:

- `skills/common/feishu_push.py`: sends text straight to a Feishu chat via
  `lark-cli im +messages-send`, reading the target chat id from
  `A_STOCK_FEISHU_CHAT_ID`. Never raises; returns a status dict
  (`sent` / `not_configured` / `empty` / `failed`) so callers can log
  telemetry without risking the underlying cron job.
- `run_agent_dag.py::target_output()` and `hermes_job_runner.py::_emit()`:
  for `deliver=feishu_direct`, push directly and always return `NO_REPLY`
  (or emit nothing) — the content never enters the agent's context window.
  `silent_when_no_signal` is still honored before this branch, unchanged
  from today's behavior.
- `generate_openclaw_cron.py::_delivery_args()`: `feishu_direct` maps to
  `--no-deliver`, same as `local`/`silent` — OpenClaw's own announce route
  must not also try to relay content this pipeline already pushed itself.
- `validate_cron_manifest.py`: `feishu_direct` added to `VALID_DELIVER`.

## Jobs migrated (`deliver: origin` → `deliver: feishu_direct`)

`capital-flow`, `event-calendar`, `official-policy-watch`, `news-monitor`,
`news-monitor-intraday`.

## Known gap — chat id not yet configured

`A_STOCK_FEISHU_CHAT_ID` is unset in this repo. Until it is set (planned to
be wired from the OpenClaw side), `feishu_push.push_text` returns
`not_configured` for every call: no message is sent, no crash, and
`push_telemetry.jsonl` records `delivered=False,
silent_reason=feishu_not_configured` so the gap is visible in
`cron_budget_report.py --push-report` rather than silently swallowed.

## Test plan

- `pytest` full suite: 832 passed
- `python scripts/validate_cron_manifest.py cron/hermes-cron-manifest.json` → OK: 39 jobs
- `python scripts/smoke_test.py` → 11 passed
- `build_openclaw_commands()` run against the real manifest confirms no
  `ValueError` for the migrated jobs
- New tests: `tests/test_feishu_push.py`, `target_output`/`_emit`
  `feishu_direct` cases in `tests/test_agent_dag.py` and
  `tests/test_hermes_job_runner.py`, manifest deliver assertions in
  `tests/test_cron_manifest.py`, OpenClaw export mapping in
  `tests/test_openclaw_cron_export.py`
