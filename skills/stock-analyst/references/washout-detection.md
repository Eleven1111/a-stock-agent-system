# Washout And Oversold Detection

Use this workflow when the request concerns washout, oversold, capitulation, or
whether a decline is stabilizing. A washout is a hypothesis to test, not a
label inferred from price decline alone.

## Evidence Matrix

| Evidence | Possible washout | Structural decline risk |
| --- | --- | --- |
| Price path | Fast 10-20% drawdown | Persistent multi-week decline |
| RSI/KDJ | Oversold then recovering | Long oversold persistence |
| Volume | Capitulation then contraction | Weak rallies and continued distribution |
| Bollinger | Reclaims lower band | Remains below lower band |
| Fundamentals | No material deterioration | Earnings, cash flow, or industry damage |
| Announcements | No unresolved hard risk | Clarification, investigation, pledge, delisting risk |

No single row proves a washout.

## Discovery

1. Load active sectors and themes from `runtime_targets.py`.
2. Add sectors with a measurable recent heat reversal from full-market data.
3. Enumerate constituents from a current provider, not a static list.
4. Record universe size, coverage, source timestamp, and exclusions.
5. Keep a bounded candidate set for expensive technical and fundamental checks.

Manual cancellation tombstones apply before discovery results are merged.

## Candidate Checks

For each candidate:

- Recent 5/10/20-day return and maximum drawdown.
- RSI, KDJ, MA alignment, Bollinger position, and volume structure.
- Latest quote and tradeability.
- Earnings, cash flow, leverage, valuation context, and industry change.
- Announcement scan for clarification and hard risks.
- Portfolio affordability and concentration only after the security passes
  evidence checks.

## Decision Rules

- Oversold below MA20 is not a buy signal by itself.
- Bearish MA alignment caps the positive technical conclusion.
- A low valuation with falling profit may be a value trap.
- A rebound without volume or with unresolved announcement risk remains
  observation-only.
- Data gaps reduce confidence and may block a directional conclusion.
- Any sell plan for an existing position must apply A-share T+1 rules.

## Output

```text
Security and evidence timestamp
Drawdown and technical state
Fundamental and announcement checks
Why washout remains plausible or is rejected
Observation trigger
Invalidation condition
Data gaps and confidence
Portfolio and T+1 constraints, if relevant
```

Do not convert a runtime sector subscription into a positive thesis. It only
defines what must be covered.
