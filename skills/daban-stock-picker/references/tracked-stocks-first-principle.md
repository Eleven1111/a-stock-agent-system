# Runtime Subscription Scan Principle

Auction and open-confirmation workflows must inspect the current runtime
subscription set before broader discovery pools. This prevents a narrow
strategy universe from hiding material movement in an explicitly monitored
stock.

The rule is procedural, not preferential:

1. Load active subscriptions through `skills/common/runtime_targets.py`.
2. Remove expired entries and manual-cancellation tombstones.
3. Inspect fresh auction or open data for those stocks.
4. Report material anomalies with the same quality and risk checks used for
   every other candidate.
5. Continue with prior-limit-up and full-market discovery pools.

An active subscription receives guaranteed coverage, not a higher score or a
waiver from announcement, tradeability, portfolio, strategy, or T+1 gates.
Codes and themes must remain in runtime state rather than this document.
