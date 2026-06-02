---
name: serenity-investment-research
description: >-
  Use this skill whenever the user asks for Serenity-style investment research,
  stock or industry research, AI supply-chain bottleneck analysis,
  semiconductor/photonics/neocloud/robotics/materials thesis work, catalyst
  mapping, valuation odds analysis, or a structured equity research report. This
  skill produces evidence-led, source-cited, non-advisory investment research
  using a deterministic pipeline including source harvesting, evidence ledger,
  financial snapshot, bottleneck mapping, bear-case audit, scorecard, and report
  linting.
---

# Serenity-Style Investment Research

This is an orchestrator skill for rigorous, Serenity-inspired investment research. It should not merely write from memory. It should first build a source-backed evidence ledger, then produce the report.

Core idea: identify large demand shifts, map the supply chain, locate constrained chokepoints, test whether the market has already priced them, model value capture and catalysts, then attack the thesis with disconfirming evidence.

Always include: `本报告仅用于研究和信息整理，不构成任何投资建议。`

## Choose the Workflow

Pick the workflow before researching:

| User request | Workflow |
|---|---|
| One company, ticker, or stock | `workflows/single_stock.md` |
| Industry chain, theme, segment, or stock pool | `workflows/industry_chain.md` |
| Multiple companies or peers | `workflows/comparison.md` |

For sector-specific work, also read the relevant playbook under `references/sector_playbooks/`. If there is no playbook, use the generic chokepoint framework in `references/serenity_methodology_research.md`.

## Deep Report Gate

For `deep` reports, do not start final writing until these artifacts exist in a target output directory:

```text
outputs/{target_slug}/
  sources/                  # downloaded or linked source files when available
  evidence.json             # source-backed evidence ledger
  scorecard.json            # weighted Serenity scorecard
  report.md                 # final report
  report_lint.json          # QA result
```

For `quick` reports, still maintain an internal evidence table in the response, but scripts are optional.

## Pipeline

### 1. Scope the Target

Infer or state:
- `target_name`
- `ticker`
- `market`
- `research_type`: `single_stock`, `industry_chain`, or `comparison`
- `research_depth`: `quick`, `standard`, or `deep`
- `time_horizon`
- relevant sector playbook

If the target is current, listed, regulated, or event-sensitive, browse for fresh sources.

### 2. Harvest Sources

Prefer S/A sources first:
- Company filings, annual reports, quarterly reports, exchange announcements.
- Investor relations records, earnings calls, prospectuses, official presentations.
- Regulator/standard bodies, government policy files, exchange data.
- Reputable market data and credible industry reports.

For A-share work, use or adapt:

```bash
python scripts/cninfo_fetch.py --stock-code 002050 --query "年年度报告" --out outputs/sanhua/sources
python scripts/pdf_extract.py outputs/sanhua/sources/report.pdf --out outputs/sanhua/sources/report.txt
```

If web search is used instead of scripts, still record every material source into the evidence ledger.

### 3. Build the Evidence Ledger

Use `templates/evidence_ledger.md` as the schema. For a deep report, create JSON:

```bash
python scripts/evidence_ledger.py init --target "三花智控" --research-type single_stock --out outputs/sanhua/evidence.json
python scripts/evidence_ledger.py add --ledger outputs/sanhua/evidence.json --claim "..." --source-title "..." --url "..." --date "2026-04-30" --grade S --claim-type fact --supports robotics_stage
python scripts/evidence_ledger.py validate outputs/sanhua/evidence.json
```

Every final core thesis must cite at least one ledger entry. Strong claims need multiple S/A entries.

### 4. Extract and Check Financials

For listed-company reports, use:

```bash
python scripts/financial_snapshot.py --text outputs/sanhua/sources/report.txt --out outputs/sanhua/financials.json
```

Manually inspect output. The script extracts obvious numbers; the analyst remains responsible for checking units, accounting period, restatements, and source context.

### 5. Map the Chokepoint

Use the Serenity five-part bottleneck test:

| Test | Required evidence |
|---|---|
| Demand confirmation | Customer capex, orders, shipments, policy, roadmap, adoption data |
| Supply constraint | Qualification cycle, patents, capacity, scarce know-how, customer switching cost |
| Market attention gap | Low coverage, stale labels, wrong comps, legacy drag, local-market neglect |
| Value capture | Revenue ramp, margin expansion, pricing power, operating leverage |
| Catalyst | Earnings, customer production, policy award, index/listing, M&A, technical milestone |

Write both the bull case and what would prove each point wrong.

### 6. Score the Thesis

Use:

```bash
python scripts/scorecard.py --out outputs/sanhua/scorecard.json \
  --industry-space 4 --business-model 4 --competition 4.5 \
  --financial-quality 4 --valuation-odds 2.5 --risk-control 3.5
```

Do not let a high qualitative story override low evidence quality or bad valuation odds.

### 7. Write and Lint the Report

Use:
- `templates/investment_research_report_template.md` for stocks.
- `templates/industry_chain_report_template.md` for industry chains.

Then run:

```bash
python scripts/report_lint.py outputs/sanhua/report.md --evidence outputs/sanhua/evidence.json --out outputs/sanhua/report_lint.json
```

If lint fails, fix the report before presenting the final answer unless the failure is a known limitation that must be disclosed.

## Source Grading

Use `references/source_grading.md`. Short version:

| Grade | Source type | Use |
|---|---|---|
| S | Direct filings, exchange disclosures, official company/IR docs, earnings calls, direct primary posts/interviews | Core evidence |
| A | Reputable media quoting primary material, regulator databases, reliable market data, mirrored direct posts with original links | Strong support |
| B | Independent summaries with traceable claims | Supporting evidence |
| C | Community discussion, screenshots, unverified social summaries | Leads only |
| D | Anonymous, broken, untraceable, contradictory claims | Do not use for conclusions |

## Output Rules

- Lead with the conclusion, then the evidence.
- Separate `fact`, `source-backed inference`, `third-party summary`, and `researcher inference`.
- Prefer tables for source lists, evidence matrices, scorecards, risk registers, catalysts, and tracking indicators.
- Do not invent identities, holdings, audited returns, private access, management commentary, customer names, target prices, or exact orders.
- For current markets and listed securities, browse or retrieve fresh data before answering.
- If data is unavailable, say what was unavailable and lower confidence.
- Do not provide personal buy/sell advice or user-specific position sizing.
- A report without a bear case, evidence table, scorecard, and tracking checklist is incomplete.

## Files

- `workflows/single_stock.md` - listed-company deep report workflow.
- `workflows/industry_chain.md` - sector and supply-chain workflow.
- `workflows/comparison.md` - peer comparison workflow.
- `references/serenity_methodology_research.md` - method abstraction and original Serenity research.
- `references/source_grading.md` - credibility and citation rules.
- `references/sector_playbooks/` - sector-specific chokepoint maps.
- `templates/` - report, evidence, and bear-case templates.
- `scripts/` - deterministic helpers for source extraction, evidence ledgers, scoring, valuation, and linting.
