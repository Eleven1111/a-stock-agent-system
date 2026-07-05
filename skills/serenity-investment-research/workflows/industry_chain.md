# Industry Chain Workflow

Use this workflow for themes such as embodied robotics, CPO, neocloud, AI semiconductors, materials, or data-center power/thermal infrastructure.

## Dual Ranking Discipline (双榜分离)

A theme scan produces **two rankings in strict order**: first the value-chain
tier ranking, then the company ranking. Never rank companies before the tiers
are ranked, and never let a hot company pull its tier upward.

### Ranking 1: value-chain tiers (产业链层级排序)

Split tiers fine-grained — compute silicon, EDA/IP, memory interconnect,
equipment, materials, test/packaging, optical link, PCB/CCL, power/thermal are
**separate tiers**, never one mixed bucket. A deep scan needs at least 3 tiers.

For every tier, argue its scarcity — who is the real expansion constraint:

| Scarcity signal | What to look for |
|---|---|
| Supplier count | How many qualified suppliers exist; 2-3 winners in tenders = real constraint |
| Qualification cycle | How long customer validation takes; longer = harder to displace |
| Expansion difficulty | Capex intensity, yield ramp, environmental permits, construction time |
| Dedicated equipment / know-how | Special tooling, process recipes, patents that cannot be bought |
| Prepayments / capacity booking | Customers paying ahead or booking capacity = demand outrunning supply |

Rank tiers by scarcity, not by market heat.

### Ranking 2: companies (公司排序)

Only after the tier ranking, rank companies inside and across tiers. Every
final candidate must answer five questions:

1. 卡住哪个环节 — which step it actually constrains.
2. 链上位置 — where it sits in the chain (tier + role).
3. 为什么排这里 — why this rank versus its neighbors.
4. 证据是什么 — evidence ledger IDs backing the claim.
5. 什么情况推翻 — what observation would overturn this rank.

## Deep-Scan Minimum Standard (hard gate)

For `research_depth = deep`, the scan is incomplete unless:

- >= 3 value-chain tiers ranked with scarcity arguments.
- Candidate universe >= 20 companies when the market is large enough.
- Evidence ledger >= 25 sources (`report_lint.py --min-sources`).
- The report contains a 「被降级的热门方向」 chapter naming at least one
  market-hot direction that ranks low, with the reason (forced anti-consensus
  check).

Below the bar: label the output 「初步结论」, list the remaining verification
items, and do not present it as a complete scan.

## Required Output

The final report must include, in this order:

1. Industry stage and commercialization clock.
2. Supply-chain map.
3. Value-chain tier ranking with per-tier scarcity argument (产业链层级排序).
4. Company ranking / watchlist by value-chain node, each final candidate
   answering the five questions (公司排序/标的池).
5. 被降级的热门方向 (downgraded hot directions).
6. Evidence matrix.
7. Catalyst calendar.
8. Bear-case and disconfirming evidence, plus a 红旗清单 section whenever the
   ledger holds `red_flag` entries.
9. Scorecard for the industry and, when useful, for each node.
10. Tracking checklist and 优先研究名单.

## Source Plan

Use current sources:

| Evidence need | Preferred source |
|---|---|
| End demand | Customer deployments, capex, shipment data, policy standards, credible industry research |
| Technical bottlenecks | Product teardowns, patents, technical papers, customer qualification statements, supplier docs |
| Commercialization | Official company announcements, customer contracts, deployments, earnings calls |
| Public-company exposure | Annual reports, IR records, prospectuses, exchange filings |
| Valuation and crowding | Market data, sell-side consensus where accessible, price performance, coverage intensity |

For A-share candidates, follow `references/a_share_verification_paths.md`:
问询函/监管函, 互动易 (read the local `stock_intelligence.py` cache first),
招投标/中标公告, 环评/能评/备案, 海关数据, 财务交叉验证, 关联交易/定增/质押.

## Evidence Tags

Use these ledger tags:

```text
industry_stage
end_demand
value_chain_node
tier_scarcity
bottleneck_candidate
commercialization
public_company_exposure
valuation_crowding
catalyst
risk
bear_case
tracking_metric
```

Record red-flag hits with `--claim-type red_flag` per `references/red_flags.md`.

## Chokepoint Ranking Method

Score each node 1-5:

| Dimension | Meaning |
|---|---|
| Usage intensity | How many units or dollars per end product |
| Technical difficulty | Precision, yield, qualification, IP, process know-how |
| Supplier scarcity | Number and quality of suppliers |
| Switching cost | Customer validation, redesign cost, reliability risk |
| Value capture | Margin, revenue ramp, price power, recurring replacement |
| Evidence quality | S/A proof versus rumor |
| Crowding penalty | Deduct if the market has already over-priced the node |

## Report Discipline

- Do not rank stocks only because they are popular in a theme.
- Distinguish `true bottleneck`, `important but competitive`, `theme proxy`, and `unverified exposure`.
- If a company only says it is "actively laying out" a field, mark exposure as
  weak unless revenue, orders, or customer validation exists; if segment data
  stays flat while management rides the theme, record a `red_flag`.
- For sector reports, include at least one section titled `What Would Prove This Theme Is Overhyped`.
- Lint with the industry gates:
  `python scripts/report_lint.py report.md --evidence evidence.json --report-type industry_chain --min-sources 25`.

## Sector Playbooks

Read the relevant playbook when available:

- `references/sector_playbooks/humanoid-robotics.md`
- `references/sector_playbooks/ai-semiconductors.md`
- `references/sector_playbooks/cpo-photonics.md`
