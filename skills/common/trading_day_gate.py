#!/usr/bin/env python3
"""Fail-closed A-share trading-day policy for scheduled jobs."""

from __future__ import annotations

import argparse
import json
from typing import Any, Mapping

from a_share_rules import CalendarCoverageError, is_trading_day


VALID_POLICIES = {"required", "calendar_day"}


def evaluate_job_trading_day(
    job: Mapping[str, Any],
    calendar_date: str,
) -> dict[str, Any]:
    """Return run, skip, or block without changing process state."""
    policy = str(job.get("trading_day_policy") or "required")
    if policy not in VALID_POLICIES:
        return {
            "action": "block",
            "calendar_date": calendar_date,
            "policy": policy,
            "reason": "invalid_policy",
        }
    if policy == "calendar_day":
        return {
            "action": "run",
            "calendar_date": calendar_date,
            "policy": policy,
            "reason": None,
        }
    try:
        trading_day = is_trading_day(calendar_date)
    except (CalendarCoverageError, ValueError):
        return {
            "action": "block",
            "calendar_date": calendar_date,
            "policy": policy,
            "reason": "calendar_uncovered",
        }
    return {
        "action": "run" if trading_day else "skip",
        "calendar_date": calendar_date,
        "policy": policy,
        "reason": None if trading_day else "non_trading_day",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("calendar_date")
    parser.add_argument("--policy", choices=sorted(VALID_POLICIES), default="required")
    args = parser.parse_args()
    result = evaluate_job_trading_day(
        {"trading_day_policy": args.policy},
        args.calendar_date,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 75 if result["action"] == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())
