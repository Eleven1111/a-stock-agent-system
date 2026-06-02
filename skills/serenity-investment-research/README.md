# Serenity Investment Research Skill

Evidence-led Codex skill for Serenity-style investment research.

The skill turns stock or industry-chain research into a repeatable workflow:

- source harvesting
- evidence ledger
- PDF extraction
- financial snapshot
- chokepoint mapping
- bear-case audit
- valuation scenarios
- weighted scorecard
- report linting

It is designed for research and information organization only. It does not provide personal financial advice.

## Structure

```text
SKILL.md
workflows/
references/
templates/
scripts/
evals/
```

## Quick Script Checks

```bash
python3 -m py_compile scripts/*.py
python3 scripts/evidence_ledger.py init --target "Example" --research-type single_stock --out /tmp/evidence.json
python3 scripts/scorecard.py --out /tmp/scorecard.json --industry-space 4 --business-model 4 --competition 3 --financial-quality 4 --valuation-odds 3 --risk-control 4
```

## Disclaimer

This project is for investment research workflows and evidence organization. It is not investment advice.
