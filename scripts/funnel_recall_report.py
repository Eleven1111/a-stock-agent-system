#!/usr/bin/env python3
"""T4 research report: discovery-funnel recall and gate regret.

Reads settled candidate_lifecycle days and answers, per gate:
- Of the stocks that turned into big movers, how many survived this gate?
  (recall — low recall means the gate throws away winners)
- What returns did the stocks this gate rejected actually go on to make?
  (regret — the alpha the gate left on the table)

Research-only: prints evidence. Changes no weight and no gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)
sys.path.insert(0, ROOT)

import lifecycle_analytics as la  # noqa: E402


def build_report(
    *,
    days: list[str] | None = None,
    outcome_key: str = "t3_close_ret",
    big_mover_threshold: float = 9.9,
) -> dict:
    settled_days = days if days is not None else la.available_days()
    records = la.load_settled_records(settled_days)
    per_day = {}
    for asof in settled_days:
        day_records = [r for r in records if r.get("asof") == asof]
        if day_records:
            per_day[asof] = la.funnel_analysis(
                day_records,
                outcome_key=outcome_key,
                big_mover_threshold=big_mover_threshold,
            )
    pooled = la.funnel_analysis(
        records,
        outcome_key=outcome_key,
        big_mover_threshold=big_mover_threshold,
    )
    recall_source_breakdown = la.recall_source_breakdown(
        records,
        outcome_key=outcome_key,
        big_mover_threshold=big_mover_threshold,
    )
    settled_day_count = len(per_day)
    return {
        "schema": "a_stock_funnel_recall_report_v1",
        "status": "ok" if records else "insufficient_data",
        "research_only": True,
        "note": (
            "Directional evidence, not conclusive: "
            f"{settled_day_count} settled trading day(s), {len(records)} records. "
            "Interpret gate recall/regret as a monitored signal that firms up as "
            "settled days accumulate; do not retune gates off a single day."
        ),
        "outcome_key": outcome_key,
        "big_mover_threshold": big_mover_threshold,
        "settled_days": sorted(per_day),
        "pooled": pooled,
        "per_day": per_day,
        "recall_source_breakdown": recall_source_breakdown,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcome", default="t3_close_ret", choices=la.OUTCOME_KEYS)
    parser.add_argument("--big-mover-threshold", type=float, default=9.9)
    parser.add_argument("--day", action="append", dest="days", help="Limit to specific asof day(s)")
    args = parser.parse_args()
    report = build_report(
        days=args.days,
        outcome_key=args.outcome,
        big_mover_threshold=args.big_mover_threshold,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
