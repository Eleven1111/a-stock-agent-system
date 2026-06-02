# Industry Chain Workflow

Use this workflow for themes such as embodied robotics, CPO, neocloud, AI semiconductors, materials, or data-center power/thermal infrastructure.

## Required Output

The final report must include:

1. Industry stage and commercialization clock.
2. Supply-chain map.
3. Chokepoint ranking.
4. Public-company watchlist by value-chain node.
5. Evidence matrix.
6. Catalyst calendar.
7. Bear-case and disconfirming evidence.
8. Scorecard for the industry and, when useful, for each node.
9. Tracking checklist.

## Source Plan

Use current sources:

| Evidence need | Preferred source |
|---|---|
| End demand | Customer deployments, capex, shipment data, policy standards, credible industry research |
| Technical bottlenecks | Product teardowns, patents, technical papers, customer qualification statements, supplier docs |
| Commercialization | Official company announcements, customer contracts, deployments, earnings calls |
| Public-company exposure | Annual reports, IR records, prospectuses, exchange filings |
| Valuation and crowding | Market data, sell-side consensus where accessible, price performance, coverage intensity |

## Evidence Tags

Use these ledger tags:

```text
industry_stage
end_demand
value_chain_node
bottleneck_candidate
commercialization
public_company_exposure
valuation_crowding
catalyst
risk
bear_case
tracking_metric
```

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
- If a company only says it is "actively laying out" a field, mark exposure as weak unless revenue, orders, or customer validation exists.
- For sector reports, include at least one section titled `What Would Prove This Theme Is Overhyped`.

## Sector Playbooks

Read the relevant playbook when available:

- `references/sector_playbooks/humanoid-robotics.md`
- `references/sector_playbooks/ai-semiconductors.md`
- `references/sector_playbooks/cpo-photonics.md`
