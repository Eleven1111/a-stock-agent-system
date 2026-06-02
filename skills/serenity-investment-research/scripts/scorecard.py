#!/usr/bin/env python3
"""Compute the Serenity weighted scorecard."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

WEIGHTS = {
    "industry_space": 0.15,
    "business_model": 0.20,
    "competition": 0.15,
    "financial_quality": 0.15,
    "valuation_odds": 0.20,
    "risk_control": 0.15,
}

LABELS = {
    "industry_space": "行业空间",
    "business_model": "商业模式",
    "competition": "竞争格局",
    "financial_quality": "财务质量",
    "valuation_odds": "估值赔率",
    "risk_control": "风险控制",
}


def rating(total: float) -> str:
    if total >= 85:
        return "强烈看多（非投资建议）"
    if total >= 70:
        return "谨慎看多（非投资建议）"
    if total >= 55:
        return "中性观察"
    if total >= 40:
        return "谨慎回避"
    return "明确回避"


def validate_score(value: float, name: str) -> None:
    if value < 1 or value > 5:
        raise SystemExit(f"{name} must be between 1 and 5")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for key in WEIGHTS:
        parser.add_argument(f"--{key.replace('_', '-')}", type=float, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    dimensions = {}
    total = 0.0
    for key, weight in WEIGHTS.items():
        value = getattr(args, key)
        validate_score(value, key)
        weighted = value / 5.0 * weight * 100.0
        total += weighted
        dimensions[key] = {
            "label": LABELS[key],
            "weight": weight,
            "score_1_to_5": value,
            "weighted_score": round(weighted, 2),
        }

    data = {
        "total": round(total, 2),
        "rating": rating(total),
        "dimensions": dimensions,
        "notes": args.notes,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"total": data["total"], "rating": data["rating"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
