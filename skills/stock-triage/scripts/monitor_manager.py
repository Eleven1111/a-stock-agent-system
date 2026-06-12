#!/usr/bin/env python3
"""Manage dynamic stock/theme/sector monitoring subscriptions."""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))

from monitor_registry import activate, active_entries, cancel  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="A股动态监控订阅管理")
    parser.add_argument("--add-stock", nargs=2, metavar=("CODE", "NAME"))
    parser.add_argument("--add-theme")
    parser.add_argument("--add-sector")
    parser.add_argument("--cancel-stock")
    parser.add_argument("--cancel-theme")
    parser.add_argument("--cancel-sector")
    parser.add_argument("--reason", default="用户明确取消")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.add_stock:
        code, name = args.add_stock
        output = activate("stock", code, name, source="manual", force=True)
    elif args.add_theme:
        output = activate("theme", args.add_theme, args.add_theme, source="manual", force=True)
    elif args.add_sector:
        output = activate("sector", args.add_sector, args.add_sector, source="manual", force=True)
    elif args.cancel_stock:
        output = cancel("stock", args.cancel_stock, args.reason, manual=True)
    elif args.cancel_theme:
        output = cancel("theme", args.cancel_theme, args.reason, manual=True)
    elif args.cancel_sector:
        output = cancel("sector", args.cancel_sector, args.reason, manual=True)
    else:
        output = {"active": active_entries()}
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
