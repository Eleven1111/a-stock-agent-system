#!/usr/bin/env python3
"""Normalize a provider JSON file and write PIT fundamental snapshots.

The script is intentionally source-agnostic.  Network/provider adapters feed
it normalized records; it never invents missing financial values.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "skills", "common"))

from fundamentals_snapshot import write_fundamental_snapshot


def _required(record: dict, field: str):
    value = record.get(field)
    if value is None or value == "":
        raise SystemExit(f"{field} is required")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="write fundamental_facts_v1 snapshots")
    parser.add_argument("--input", required=True, help="JSON object or list of normalized records")
    parser.add_argument("--trading-date", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--producer", default="fundamentals_snapshot")
    parser.add_argument("--producer-version", default="fundamental_facts_v1")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    records = payload if isinstance(payload, list) else [payload]
    written = []
    for record in records:
        if not isinstance(record, dict):
            raise SystemExit("each input record must be an object")
        code = str(record.get("code") or "")
        if not code:
            raise SystemExit("input record code is required")
        written.append(write_fundamental_snapshot(
            code,
            record,
            trading_date=args.trading_date,
            batch_id=args.batch_id,
            producer=args.producer,
            producer_version=args.producer_version,
            source_versions=record.get("source_versions"),
            event_time=_required(record, "event_time"),
            published_at=_required(record, "published_at"),
            available_at=_required(record, "available_at"),
            captured_at=_required(record, "captured_at"),
            watermark=_required(record, "watermark"),
            sealed_at=_required(record, "sealed_at"),
        ))
    print(json.dumps({"ok": True, "count": len(written), "snapshots": written}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
