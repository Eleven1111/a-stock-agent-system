#!/usr/bin/env python3
"""Best-effort extraction of financial metrics from filing text."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


METRICS = {
    "revenue": [r"营业收入[（(]元[）)]\s*([0-9,.\-]+)", r"营业收入\s+([0-9,.\-]+)"],
    "net_profit_parent": [
        r"归属于上市公司股东\s*的净利润[（(]元[）)]\s*([0-9,.\-]+)",
        r"归属于上市公司股东的净利\s*润[（(]元[）)]\s*([0-9,.\-]+)",
        r"归属于上市公司股东的净利润[（(]元[）)]\s*([0-9,.\-]+)",
    ],
    "net_profit_parent_ex_items": [
        r"归属于上市公司股东\s*的扣除非经常性损益\s*的净利润[（(]元[）)]?\s*([0-9,.\-]+)",
        r"归属于上市公司股东的扣除非经常性损益的净利\s*润\s*[（(]元[）)]?\s*([0-9,.\-]+)",
        r"归属于上市公司股东的扣除非经常性损益的净利润\s*[（(]元[）)]?\s*([0-9,.\-]+)",
    ],
    "operating_cash_flow": [
        r"经营活动产生的现金\s*流量净额[（(]元[）)]\s*([0-9,.\-]+)",
        r"经营活动产生的现金流量净\s*额[（(]元[）)]\s*([0-9,.\-]+)",
        r"经营活动产生的现金流量净额[（(]元[）)]\s*([0-9,.\-]+)",
    ],
    "total_assets": [r"总资产[（(]元[）)]\s*([0-9,.\-]+)"],
    "equity_parent": [
        r"归属于上市公司股东\s*的净资产[（(]元[）)]\s*([0-9,.\-]+)",
        r"归属于上市公司股东的所有\s*者权益[（(]元[）)]\s*([0-9,.\-]+)",
        r"归属于上市公司股东的所有者权益[（(]元[）)]\s*([0-9,.\-]+)",
        r"归属于上市公司股东的净资产[（(]元[）)]\s*([0-9,.\-]+)",
    ],
    "eps_basic": [r"基本每股收益[（(]元/\s*股[）)]\s*([0-9,.\-]+)", r"基本每股收益[（(]元/股[）)]\s*([0-9,.\-]+)"],
    "roe_weighted": [r"加权平均净资产收益\s*率\s*([0-9,.\-]+%)", r"加权平均净资产收益率\s*([0-9,.\-]+%)"],
    "rd_expense": [r"研发费用\s+([0-9,.\-]+)"],
}


def parse_number(raw: str) -> float | str:
    raw = raw.strip().replace(",", "")
    if raw.endswith("%"):
        try:
            return float(raw[:-1]) / 100.0
        except ValueError:
            return raw
    try:
        return float(raw)
    except ValueError:
        return raw


def first_match(text: str, patterns: list[str]) -> float | str | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return parse_number(match.group(1))
    return None


def extract_segment_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pattern = re.compile(
        r"(制冷空调电器零部件业务|汽车零部件业务)\s+([0-9,.\-]+)\s+([0-9,.\-]+)\s+([0-9,.\-]+)\s+([0-9,.\-]+)"
    )
    for m in pattern.finditer(text):
        rows.append(
            {
                "segment": m.group(1),
                "revenue": parse_number(m.group(2)),
                "cost": parse_number(m.group(3)),
                "assets": parse_number(m.group(4)),
                "liabilities": parse_number(m.group(5)),
                "unit_note": "as printed in source table; verify whether yuan or ten-thousand yuan",
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", required=True, help="filing text file")
    parser.add_argument("--out", required=True, help="output JSON path")
    parser.add_argument("--source-title", default="")
    parser.add_argument("--source-date", default="")
    args = parser.parse_args()

    text_path = Path(args.text)
    text = text_path.read_text(encoding="utf-8", errors="replace")
    metrics = {name: first_match(text, patterns) for name, patterns in METRICS.items()}
    data = {
        "source_text": str(text_path),
        "source_title": args.source_title,
        "source_date": args.source_date,
        "unit_warning": "Numbers are extracted by pattern matching. Verify units and context before using in a report.",
        "metrics": metrics,
        "segments": extract_segment_rows(text),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
