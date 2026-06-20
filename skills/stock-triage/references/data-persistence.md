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
