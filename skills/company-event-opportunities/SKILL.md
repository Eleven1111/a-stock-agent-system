# Company Event Opportunities

Scan existing A-share runtime evidence for company-specific event opportunities
and risks. This skill is deterministic and scheduler-safe: it reads runtime
targets, local artifacts/caches, and writes structured watch/review/avoid
outputs only.

## Discipline

- Do not create buy or sell recommendations.
- Suggestions are limited to `watch`, `review`, and `avoid`.
- Leave `success_probability` and `upside_pct` as `null` unless supported by
  explicit evidence.
- Always expose event-failure downside as `downside_pct` or an unavailable risk
  flag.
- Directional use still requires announcement, data-quality, tradeability,
  price-plan, portfolio-risk, research-gate, and T+1 checks.

## Scheduler

Run through the DAG manifest:

```bash
python scripts/run_agent_dag.py company-event-opportunity-scan --emit-target
```

The isolated business command is:

```bash
python skills/company-event-opportunities/scripts/scan.py --scope runtime --json
```
