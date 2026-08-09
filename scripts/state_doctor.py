#!/usr/bin/env python3
"""Validate the configured runtime state root and recover critical JSON files."""

from __future__ import annotations

import argparse
import json
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from paths import data_file  # noqa: E402
import signal_ledger  # noqa: E402
from state_integrity import _state_root, ensure_state_identity  # noqa: E402
from state_store import CRITICAL_JSON_FILES, read_json  # noqa: E402


def _identity_candidate_roots() -> list[str]:
    """Well-known locations where a state_identity.json may have been minted."""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    candidates = [
        _state_root(os.environ),
        os.path.join(home, ".hermes"),
        os.path.join(home, ".a-stock-agent-cc"),
        ROOT,
    ]
    seen: set[str] = set()
    unique: list[str] = []
    for root in candidates:
        resolved = os.path.abspath(os.path.expanduser(root))
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def detect_split_brain() -> dict:
    """Scan candidate roots for divergent minted identities (report only)."""
    found = []
    for root in _identity_candidate_roots():
        identity_path = os.path.join(root, "state_identity.json")
        data = read_json(identity_path, None)
        if isinstance(data, dict) and data.get("state_id"):
            found.append({
                "root": root,
                "path": identity_path,
                "state_id": str(data.get("state_id")),
                "created_at": data.get("created_at"),
                "initial_root": data.get("initial_root"),
            })
    distinct_ids = {entry["state_id"] for entry in found}
    return {
        "detected": len(distinct_ids) > 1,
        "distinct_state_ids": sorted(distinct_ids),
        "identities": found,
    }


def inspect_state(runtime: str, recover: bool = False) -> dict:
    identity = ensure_state_identity(runtime)
    split_brain = detect_split_brain()
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
    status = identity["status"]
    if status == "ok" and split_brain["detected"]:
        # Diagnostic tool: report the divergence, do not hard-block.
        status = "degraded"
    return {
        "schema": "a_stock_state_doctor_v1",
        "status": status,
        "identity": identity,
        "split_brain": split_brain,
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
