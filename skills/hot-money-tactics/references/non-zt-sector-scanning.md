# Non-Limit-Up Sector Trend Scan

A limit-up pool does not represent all market leadership. Institution-led
trends may have broad participation and high turnover without any stock closing
at the limit.

## When To Run

Run the trend scan alongside the limit-up scan when:

- the request asks for market-wide sector leadership;
- the limit-up pool is unusually small;
- large-cap leaders show material moves;
- commodity, policy, or global inputs imply a sector transmission path.

## Workflow

1. Enumerate current industry and concept boards from a provider adapter.
2. Add active runtime sector/theme subscriptions.
3. Pull current constituents from the provider; do not use a static list.
4. Batch current quotes and calculate breadth, median return, turnover, and
   concentration.
5. Compare with recent limit-up counts and prior run snapshots.
6. For the top bounded sectors, fetch K-line, announcement, and risk evidence
   for representative securities.

## Sector Metrics

- advancing share and median return;
- total and median turnover;
- leader contribution versus broad participation;
- number and quality of limit-up stocks;
- multi-day persistence and reversal;
- cross-market or commodity confirmation;
- source coverage and stale-data ratio.

A sector with one strong leader and weak breadth is not equivalent to broad
sector strength.

## Security Filters

Positive trend evidence should still be rejected or downgraded for:

- excessive short-term extension;
- weak liquidity or untradeable state;
- unresolved announcement risk;
- deteriorating profit or cash flow;
- portfolio concentration limits;
- missing or stale required datasets.

## Output

Report the scanned universe, source timestamp, coverage, ranked sectors,
breadth, representative securities, exclusions, and uncertainty. The scan
produces evidence for Triage; it does not bypass strategy or portfolio policy.
