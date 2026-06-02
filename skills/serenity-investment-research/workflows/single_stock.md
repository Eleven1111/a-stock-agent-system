# Single Stock Workflow

Use this workflow for one listed company, ticker, or security.

## Output Directory

Create a stable slug:

```text
outputs/{ticker_or_name}/
  sources/
  evidence.json
  financials.json
  scorecard.json
  report.md
  report_lint.json
```

## Step 1: Source Plan

Collect at least:

| Source | Minimum for deep report |
|---|---|
| Latest annual report | Required |
| Latest quarterly or interim report | Required |
| Latest investor relations / earnings call | Required if available |
| Market data / valuation | Required and date-stamped |
| Industry or customer evidence | Required for chokepoint claims |
| Bear-case or risk evidence | Required |

For A-shares, use CNINFO/SZSE/SSE/HKEX announcements before media summaries.

## Step 2: Evidence Ledger

Initialize the ledger:

```bash
python scripts/evidence_ledger.py init --target "{target}" --research-type single_stock --out outputs/{slug}/evidence.json
```

Add evidence as it is found. Do not wait until writing. Core claims need specific `supports` tags such as:

```text
business_profile
revenue_mix
financial_quality
supply_chain_position
bottleneck_strength
market_attention
valuation
catalyst
risk
bear_case
tracking_metric
```

## Step 3: Financial Snapshot

Extract numbers from filings when possible:

```bash
python scripts/pdf_extract.py outputs/{slug}/sources/latest_annual.pdf --out outputs/{slug}/sources/latest_annual.txt
python scripts/financial_snapshot.py --text outputs/{slug}/sources/latest_annual.txt --out outputs/{slug}/financials.json
```

Check units manually. Chinese annual reports may mix yuan, ten-thousand yuan, and hundred-million yuan in different tables.

## Step 4: Chokepoint Memo

Write a compact memo before the final report:

| Test | Claim | Evidence IDs | Confidence | What would disprove it |
|---|---|---|---|---|
| Demand |  |  |  |  |
| Supply |  |  |  |  |
| Attention gap |  |  |  |  |
| Value capture |  |  |  |  |
| Catalyst |  |  |  |  |

## Step 5: Valuation and Scenarios

Use at least bear/base/bull:

```bash
python scripts/valuation_scenarios.py --out outputs/{slug}/valuation.json \
  --shares 42.08 --price 44.72 --currency CNY --unit "CNY 100m" \
  --bear-profit 40 --bear-multiple 28 \
  --base-profit 48 --base-multiple 36 \
  --bull-profit 60 --bull-multiple 40
```

All inputs must disclose unit, year, and source. Prefer direct `--market-cap` when share-count units are ambiguous.

## Step 6: Score

Score evidence, not enthusiasm:

```bash
python scripts/scorecard.py --out outputs/{slug}/scorecard.json \
  --industry-space 0 --business-model 0 --competition 0 \
  --financial-quality 0 --valuation-odds 0 --risk-control 0
```

Use `0` only as a placeholder while drafting; final scores must be 1-5.

## Step 7: Report and Lint

Write `report.md` using `templates/investment_research_report_template.md`.

Run:

```bash
python scripts/report_lint.py outputs/{slug}/report.md --evidence outputs/{slug}/evidence.json --out outputs/{slug}/report_lint.json
```

Fix all high-severity failures before final delivery.

## Final Response Shape

In chat, summarize:
- Conclusion and rating.
- Top 3 evidence-backed thesis points.
- Top 3 bear-case points.
- Scorecard total.
- Files created if any.
- Verification/lint result.
