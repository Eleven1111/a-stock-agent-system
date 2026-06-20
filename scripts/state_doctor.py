#!/usr/bin/env python3
"""Validate the configured runtime state root and recover critical JSON files."""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

from paths import data_file  # noqa: E402
import signal_ledger  # noqa: E402
from state_integrity import ensure_state_identity  # noqa: E402
from state_store import CRITICAL_JSON_FILES, read_json  # noqa: E402


def inspect_state(runtime: str, recover: bool = False) -> dict:
    identity = ensure_state_identity(runtime)
    files = []
    for filename in sorted(CRITICAL_JSON_FILES):
        path = data_file("stock-triage", filename)
        existed = os.path.exists(path)
        value = read_json(path, None) if recover or existed else None
        files.append({
            "file": filename,
            "path": path,
            "exists": os.path.exists(path),
            "recovered": not existed and os.path.exists(path),
            "valid_json": value is not None if os.path.exists(path) else None,
        })
    ledger_path = signal_ledger.LEDGER_FILE
    ledger_existed = os.path.exists(ledger_path)
    events = signal_ledger.read_events(ledger_path) if recover or ledger_existed else []
    mirror = signal_ledger.sync_backup(ledger_path) if recover and events else None
    files.append({
        "file": "signal_ledger.jsonl",
        "path": ledger_path,
        "exists": os.path.exists(ledger_path),
        "recovered": not ledger_existed and os.path.exists(ledger_path),
        "valid_events": len(events),
        "backup_path": mirror,
    })
    return {
        "schema": "a_stock_state_doctor_v1",
        "status": identity["status"],
        "identity": identity,
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=["hermes", "openclaw", "local"], default="local")
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    report = inspect_state(args.runtime, recover=args.recover)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
