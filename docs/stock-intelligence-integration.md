# Stock Intelligence Integration

The project selectively reuses endpoint knowledge and field mappings from
[`simonlin1212/a-stock-data`](https://github.com/simonlin1212/a-stock-data),
version `3.2.2`, commit `9379ab90d0219312b5f4845cd8c97502f40b0806`.

The upstream project is licensed under Apache License 2.0. Its request examples
were not copied as a runtime dependency. They were adapted to this repository's
standard-library HTTP client, provider configuration, typed failures,
cross-process rate limiter, immutable snapshots, and normalized schemas.

Integrated datasets:

- Eastmoney restricted-share releases (`RPT_LIFT_STAGE`)
- Margin financing and securities lending (`RPTA_WEB_RZRQ_GGMX`)
- Shareholder-count history (`RPT_HOLDERNUMLATEST`)
- Dragon Tiger List records and seats
- Block trades (`RPT_DATA_BLOCKTRADE`)
- Eastmoney broker-report metadata and forecast EPS fields

Operational rules:

- All Eastmoney calls pass through the shared provider limiter.
- Holdings and only the top five dynamic candidates are refreshed after close.
- Online auction/open tasks read cached evidence and never fan out live requests.
- Every refresh writes an immutable, source-versioned market snapshot.
- Partial provider failures are recorded as missing datasets.
- Stale evidence is disclosed and cannot trigger a hard veto.
- A restricted-share release of at least 10% within 30 days is a policy veto.
- Broker consensus is supporting evidence only; filings remain authoritative.
