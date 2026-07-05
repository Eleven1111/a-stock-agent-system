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

## Request Routing (read this first)

Classify the request into one of five modes before doing anything else. Only the
first three run the deterministic pipeline and create an outputs directory; the
last two are light methodology-conversation modes and must not spin up the
pipeline or write files.

| Mode | Trigger | What to do |
|---|---|---|
| theme scan (主题扫描) | industry chain, theme, segment, stock pool | run `workflows/industry_chain.md` at the requested `research_depth` |
| single-company challenge (单公司挑战) | one company, ticker, "这家公司行不行" | run `workflows/single_stock.md` |
| candidate comparison (对比) | multiple companies or peers | run `workflows/comparison.md` |
| research partner conversation (研究伙伴对话) | method question, "怎么看", "这个逻辑成立吗", follow-up on prior report | answer from methodology; cite framework/references; do NOT build outputs or run scripts |
| learning mode (学习模式) | "解释一下 chokepoint", "教我怎么排产业链" | explain the method using `references/`; no pipeline, no files |

For the three pipeline modes, also read the relevant playbook under
`references/sector_playbooks/`. If there is no playbook, use the generic
chokepoint framework in `references/serenity_methodology_research.md`. For A-share
targets read `references/a_share_verification_paths.md`.

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

## Deep Theme-Scan Minimum Standard (hard gate)

A `deep` theme scan (industry_chain) is not "complete" unless all of the
following hold. Below the bar, label the result 「初步结论」, list the remaining
checks, and do not present it as a finished scan.

- At least 3 value-chain tiers, kept separate (compute silicon, EDA/IP, memory
  interconnect, equipment, materials, test/packaging, optical link, PCB/CCL,
  power/thermal — never a single mixed bucket).
- Candidate universe >= 20 companies when the market is large enough to support it.
- Evidence ledger >= 25 sources.
- A 「被降级的热门方向」 chapter that names at least one market-hot direction that
  ranks low and explains why (forced anti-consensus check).
- Tier ranking (with a per-tier scarcity argument) is written **before** the
  company ranking. Scarcity signals: supplier count, qualification cycle,
  expansion difficulty, dedicated equipment/know-how, prepayments/capacity booking.

`report_lint.py --report-type industry_chain` enforces the source count, the
downgraded-direction chapter, the tier-before-company ordering, and red-flag
disclosure. single_stock reports are exempt from these four.

## Red Flags (硬约束)

Maintain a red-flag checklist per `references/red_flags.md`. Record every hit in
the evidence ledger with `--claim-type red_flag`. Any red-flag hit must lower the
scorecard's `financial_quality` and/or `risk_control`; per the Mainline Policy
Role a score `<= 2/5` on either is a hard negative. The report must list every
recorded red_flag entry (报告需列出全部红旗条目).

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
python ../common/stock_intelligence.py read --code 002050 --json
```

The shared stock-intelligence cache is refreshed for all holdings and only the
top five dynamic candidates. Treat it as supporting evidence, not as a
replacement for filings:

- `lockups`: upcoming restricted-share releases and dilution/supply pressure.
- `margin_trading`: leverage expansion or contraction.
- `holder_changes`: quarterly shareholder-count concentration trend.
- `dragon_tiger`: recent billboard records and institutional-seat net flow.
- `block_trades`: negotiated-trade discounts and counterparties.
- `reports`: broker report metadata and the traceable EPS consensus sample.
- `interactive_qa`: recent investor interactive Q&A (互动易/上证e互动),
  retained per stock (default last 10). Company replies are grade B
  supporting evidence; the investor question alone is an attention/lead
  signal only, never a citable fact. Shanghai coverage is best-effort and
  may report `sse_unavailable`.

Record the snapshot reference and upstream source in `evidence.json`. Broker
reports and consensus estimates are grade B supporting evidence; company
filings and exchange announcements remain the source of truth. A consensus
sample below three institutions must be disclosed as thin coverage.

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
python scripts/report_lint.py outputs/sanhua/report.md --evidence outputs/sanhua/evidence.json \
  --report-type industry_chain --min-sources 25 --out outputs/sanhua/report_lint.json
```

`--report-type auto` infers the type from `evidence.research_type`. For a large
market you may raise `--min-sources`; only lower it for a genuinely small universe
and disclose that in the report. If lint fails, fix the report before presenting
the final answer unless the failure is a known limitation that must be disclosed.

### 8. Cache the Scorecard for the Four-Dim Deep Dimension（回流四维深度面）

After producing `scorecard.json` (and optionally `valuation_scenarios.json`), write it to the
shared deep-research cache so `four_dim_scorer` 的深度面 (20%) consumes real Serenity research
instead of a PE bucket. Deep research runs once; daily scoring reuses it (freshness-decayed).

```bash
python ../../common/deep_research_cache.py write \
  --code 600519 --name 贵州茅台 \
  --scorecard outputs/tongfu/scorecard.json \
  --valuation outputs/tongfu/valuation.json \
  --asof 2026-06-09
```

The four-dim scorer reads this cache via `read_deep_research(code)`; entries older than 90 days
decay toward the PE snapshot. Re-run this write whenever a fresh report or major catalyst lands.

### 9. Close an Automatic Refresh Request

When Hermes/OpenClaw starts this skill from `serenity_refresh_requests`, claim the request before
researching and complete it only after the report passed lint and the cache write above succeeded:

```bash
python ../../common/serenity_refresh_queue.py claim --worker hermes
python ../../common/serenity_refresh_queue.py complete --id serenity-600519-2026-06-13
```

On failure, call `fail --id ... --error ...` so another run can retry. Claims are leases rather
than permanent locks; abandoned claims return to the queue after two hours. Never mark a request
complete merely because a draft exists: `complete` verifies a cache whose `asof` is at least the
request date.

### Mainline Policy Role

In the live architecture this skill is the **Deep Research Evidence / Risk** layer, not a
realtime stock picker:

- Daban candidates do not require Serenity coverage; missing research alone does not block a trade.
- Existing research is converted to structured `research_evidence_v1` and attached to the
  recommendation and Signal Ledger.
- `financial_quality <= 2/5` or `risk_control <= 2/5` is a hard negative and blocks positive action.
- Stale Serenity evidence halves the position multiplier for the trend lane.
- A positive scorecard supports explanation and sizing only. It never bypasses announcement QC,
  tradeability, Chanlun admission, portfolio concentration, or A-share T+1 rules.

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

- Lead with the conclusion, then the evidence. 先给判断再给论证，不要报告腔铺垫。
- Write direct Chinese, not translated jargon: 「产业链卡点」 not chokepoint 直译,
  「市场可能没看清的地方」 not mispricing 直译, 「接下来可能让市场重新定价的事情」 not
  catalyst 直译, 「什么情况说明这个判断错了」 not invalidation 直译. Close a theme
  scan with a 「优先研究名单」.
- Every final theme-scan candidate must answer five questions: 卡住哪个环节 /
  链上位置 / 为什么排这里 / 证据是什么 / 什么情况推翻.
- Separate `fact`, `source-backed inference`, `third-party summary`, `researcher inference`, and `red_flag`.
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
- `references/a_share_verification_paths.md` - A-share evidence-hunting paths (问询函, 互动易, 招投标, 海关, 财务交叉验证).
- `references/red_flags.md` - red-flag checklist and scoring consequences.
- `references/sector_playbooks/` - sector-specific chokepoint maps.
- `templates/` - report, evidence, and bear-case templates.
- `scripts/` - deterministic helpers for source extraction, evidence ledgers, scoring, valuation, and linting.
