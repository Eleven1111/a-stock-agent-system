# Runtime State Persistence

Conversation history is not an account ledger. Holdings, cash, trades,
recommendations, subscriptions, and settlement state must be persisted through
the shared state modules.

## State Root

All runtime paths are resolved through `skills/common/paths.py`.

- One machine, two runtimes: Hermes and OpenClaw use the same
  `A_STOCK_STATE_HOME`.
- Multiple machines: use a shared state store with verified lease semantics.
- Repository files are code and defaults; runtime state is not copied into Git.

## Canonical Records

| Record | Purpose |
| --- | --- |
| `portfolio.json` | Current cash and positions |
| `trade_history.json` | Closed and historical trades |
| `cash_flow.json` | Deposits, withdrawals, buys, and sells |
| `monitor_registry.json` | Stock, sector, and theme lifecycle |
| `signal_ledger.jsonl` | Recommendation, signal, trade, monitor, and settlement events |
| `recommendations.json` | Query projection for recommendation audit |

The Signal Ledger is append-only. Other files may be projections or mutable
state but must not create conflicting event histories.

## Signal Ledger Archiving

`monitor.*` churn is ~99% of the ledger and grows 500–800 events/day, so every
process that replays it degrades with calendar time (issue #167).
`python skills/common/signal_ledger_archive.py` moves out only the `monitor.*` events
that are both older than the retention window (default 7 days) and fully
superseded by a later event for the same monitor, into
`signal_ledger.archive/YYYY-MM.jsonl` next to the ledger.

- Never archived: `recommendation.*`, `paper.*`, `signal.*`, `tail_close.*`.
- The folded monitor projection is bit-identical before and after; the script
  verifies this per run and aborts without touching a file if it is not.
- `--dry-run` is the default. `--apply` snapshots the ledger and its backup
  mirror to `*.pre-archive-<timestamp>` first and prints the rollback commands.
- Stop the scheduler before `--apply`; a rollback loses events appended after
  the snapshot.

## Writes

- Use `mutate_json()` for read-modify-write transactions.
- Use atomic writers for complete replacements.
- Use idempotency keys for events and scheduled artifacts.
- Do not write state with ad hoc shell redirection.
- A manual monitor cancellation remains a tombstone until explicitly restored.

## Recovery

Critical mutable JSON files keep bounded versioned snapshots under
`A_STOCK_BACKUP_HOME` (or a sibling `*-backups` directory by default). Cache
files are intentionally excluded. The canonical `signal_ledger.jsonl` uses an
idempotent append-only mirror in the same independent backup root.

1. Stop writers or acquire the appropriate lease.
2. Run `python scripts/state_doctor.py --runtime openclaw --recover`.
3. Preserve the damaged file and relevant artifacts.
4. Rebuild projections from canonical events when supported.
5. Validate balances, position quantities, and lifecycle links.
6. Resume scheduled work only after consistency checks pass.

Do not reconstruct account facts from remembered conversation text.
