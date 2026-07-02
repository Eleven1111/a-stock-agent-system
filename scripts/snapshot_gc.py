#!/usr/bin/env python3
"""Plan or apply the configured snapshot and cron-artifact retention policy."""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

from storage_retention import cleanup_storage  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Delete selected files")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable result")
    parser.add_argument("--state-home", help="Override A_STOCK_STATE_HOME for maintenance")
    args = parser.parse_args()

    result = cleanup_storage(state_home=args.state_home, apply=args.apply)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"{result['mode']}: snapshots="
        f"{result['deleted']['expired_snapshots'] + result['deleted']['size_cap_snapshots']}, "
        f"cron_artifacts={result['deleted']['cron_artifacts']}, "
        f"archived={result['archived']['count']}, "
        f"reclaimed_bytes={result['reclaimed_bytes']}"
    )


if __name__ == "__main__":
    main()
