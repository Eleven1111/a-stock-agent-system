#!/usr/bin/env python3
"""
Trading-day gate for OpenClaw cron jobs.

Usage in cron payload message:
    python skills/common/trading_day_gate.py && <your actual command>

Exit 0 → trading day, proceed
Exit 1 → not a trading day, cron should skip silently

Supports --force to override (for testing).
"""
import sys
from datetime import date
from pathlib import Path

# Add skills/common to path
sys.path.insert(0, str(Path(__file__).parent))

from a_share_rules import is_trading_day, CalendarCoverageError


def main():
    if "--force" in sys.argv:
        print("force=true, skipping gate")
        return 0

    today = date.today()
    try:
        if is_trading_day(today):
            return 0
        else:
            print(f"⛔ {today.isoformat()} is not a trading day (holiday/weekend), skipping.")
            return 1
    except CalendarCoverageError as e:
        print(f"⚠️ Calendar coverage error: {e}")
        # Fail open on coverage gap — better to run than to silently skip
        return 0


if __name__ == "__main__":
    sys.exit(main())
