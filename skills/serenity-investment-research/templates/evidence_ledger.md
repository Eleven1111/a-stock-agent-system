# Evidence Ledger Template

Use this schema for `evidence.json`.

```json
{
  "target": "Company or industry",
  "research_type": "single_stock",
  "created_at": "2026-06-01T00:00:00Z",
  "entries": [
    {
      "id": "E001",
      "claim": "A precise claim supported by the source.",
      "claim_type": "fact",
      "source_title": "2025 Annual Report",
      "source_type": "annual_report",
      "url": "https://...",
      "local_path": "outputs/target/sources/report.pdf",
      "date": "2026-03-24",
      "grade": "S",
      "excerpt": "Short excerpt or paraphrase within copyright limits.",
      "supports": ["financial_quality", "revenue_mix"],
      "confidence": "high",
      "notes": ""
    }
  ]
}
```

## Required Fields

| Field | Required | Notes |
|---|---:|---|
| `id` | yes | Stable evidence id, e.g. E001 |
| `claim` | yes | One atomic claim |
| `claim_type` | yes | fact / source-backed inference / third-party summary / researcher inference |
| `source_title` | yes | Human-readable source name |
| `date` | yes | Publication or filing date when available |
| `grade` | yes | S/A/B/C/D |
| `supports` | yes | Tags used by report sections |
| `confidence` | yes | high / medium / low |

## Rules

- One ledger entry supports one atomic claim.
- Do not put broad thesis paragraphs into `claim`.
- Do not use D-grade entries in final conclusions.
- If the source is a PDF, include `local_path` after download.
