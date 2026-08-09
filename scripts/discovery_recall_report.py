#!/usr/bin/env python3
"""Persist the daily D0 -> auction -> open recall quality report.

This report consumes already-captured artifacts.  It performs no market scan
and never changes the candidate, auction, or execution pools.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Any, Mapping

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
AUCTION_SCRIPTS = os.path.join(ROOT, "skills", "daban-stock-picker", "scripts")
sys.path.insert(0, COMMON)
sys.path.insert(0, AUCTION_SCRIPTS)

import auction_collector  # noqa: E402
import candidate_pipeline  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402


def _codes(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [
        candidate_pipeline.naked_code(item.get("code") if isinstance(item, Mapping) else item)
        for item in items
        if (item.get("code") if isinstance(item, Mapping) else item)
    ]


def build_report(asof: str) -> dict[str, Any]:
    pool = read_json(data_file("stock-triage", "candidate_pool_latest.json"), {})
    state = read_json(data_file("daban-stock-picker", f"auction_{asof}.json"), {})
    shortlist = read_json(
        data_file("daban-stock-picker", f"auction_shortlist_{asof}.json"),
    )
    opened = read_json(
        data_file("daban-stock-picker", f"open_confirmation_{asof}.json"),
    )
    if not isinstance(pool, Mapping) or str(pool.get("asof") or "") != asof:
        return {
            "schema": "discovery_recall_report_v1",
            "asof": asof,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "blocked",
            "error": "candidate_pool_missing_or_stale",
            "execution_gate_unchanged": True,
        }
    series = state.get("series") if isinstance(state, Mapping) else {}
    rows = []
    if isinstance(series, Mapping):
        for snapshots in series.values():
            if not isinstance(snapshots, list):
                continue
            rows.extend(
                dict(snapshot)
                for snapshot in snapshots
                if isinstance(snapshot, Mapping)
                and snapshot.get("snapshot_scope") == "full_market"
            )
    if not rows:
        return {
            "schema": "discovery_recall_report_v1",
            "asof": asof,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "blocked",
            "error": "09:24_full_market_snapshot_missing",
            "execution_gate_unchanged": True,
        }

    # The report is generated after open confirmation.  A stale open artifact
    # is treated as unavailable rather than silently counted as zero recall.
    open_ready = (
        isinstance(opened, Mapping)
        and str(opened.get("asof") or "") == asof
        and str(opened.get("status") or "") == "ready"
    )
    open_codes = _codes(opened.get("signals")) if open_ready else None
    shortlist_codes = _codes(shortlist.get("shortlist")) if isinstance(shortlist, Mapping) else []
    report = auction_collector.build_discovery_recall_report(
        rows,
        prefilter_codes=pool.get("prefilter_codes") or [],
        auction_codes=[
            item.get("code")
            for item in pool.get("candidates") or []
            if isinstance(item, Mapping) and item.get("code")
        ],
        executable_codes=shortlist_codes or None,
        open_codes=open_codes,
        asof=asof,
        source_stage="09:24_full_market_snapshot",
    )
    report["full_market_snapshot_count"] = len(rows)
    report["pool_asof"] = pool.get("asof")
    report["shortlist_asof"] = shortlist.get("asof") if isinstance(shortlist, Mapping) else None
    report["open_asof"] = opened.get("asof") if isinstance(opened, Mapping) else None
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="A股池外强票召回损失报告")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(args.asof)
    atomic_write_json(
        data_file("stock-triage", "discovery_recall_report_latest.json"),
        report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else report["status"])


if __name__ == "__main__":
    main()
