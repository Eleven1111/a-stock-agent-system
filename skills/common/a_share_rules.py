"""A-share trading calendar and T+1 execution constraints."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any


CALENDAR_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "a_share_calendar.json")
)


class CalendarCoverageError(RuntimeError):
    """Raised when an execution decision falls outside the verified calendar."""


def _as_date(value: date | datetime | str | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


@lru_cache(maxsize=1)
def _calendar() -> dict[str, Any]:
    try:
        with open(CALENDAR_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        payload = {}
    payload["closed_dates"] = set(payload.get("closed_dates") or [])
    payload["covered_years"] = set(payload.get("covered_years") or [])
    return payload


def _ensure_covered(day: date) -> None:
    if day.year not in _calendar()["covered_years"]:
        raise CalendarCoverageError(
            f"A-share calendar does not cover {day.year}; refresh config/a_share_calendar.json"
        )


def is_trading_day(value: date | datetime | str) -> bool:
    day = _as_date(value)
    _ensure_covered(day)
    if day.weekday() >= 5:
        return False
    return day.isoformat() not in _calendar()["closed_dates"]


def next_trading_day(value: date | datetime | str) -> date:
    day = _as_date(value)
    _ensure_covered(day)
    for offset in range(1, 32):
        candidate = day + timedelta(days=offset)
        if is_trading_day(candidate):
            return candidate
    raise RuntimeError(f"无法在 {day.isoformat()} 后 31 天内找到交易日")


def previous_trading_day(value: date | datetime | str) -> date:
    day = _as_date(value)
    _ensure_covered(day)
    for offset in range(1, 32):
        candidate = day - timedelta(days=offset)
        if is_trading_day(candidate):
            return candidate
    raise RuntimeError(f"无法在 {day.isoformat()} 前 31 天内找到交易日")


def latest_trading_day(value: date | datetime | str | None = None) -> date:
    day = _as_date(value)
    if is_trading_day(day):
        return day
    return previous_trading_day(day + timedelta(days=1))


def add_trading_days(value: date | datetime | str, count: int) -> date:
    if count < 0:
        raise ValueError("count must be non-negative")
    current = _as_date(value)
    _ensure_covered(current)
    for _ in range(count):
        current = next_trading_day(current)
    return current


def t1_constraint(
    acquired_on: date | datetime | str | None,
    asof: date | datetime | str | None = None,
) -> dict[str, Any]:
    acquired = _as_date(acquired_on)
    current = _as_date(asof)
    _ensure_covered(acquired)
    _ensure_covered(current)
    earliest = next_trading_day(acquired)
    return {
        "market": "A_SHARE",
        "settlement_rule": "T+1",
        "same_day_sell_allowed": False,
        "acquired_on": acquired.isoformat(),
        "earliest_sell_date": earliest.isoformat(),
        "sell_allowed": current >= earliest,
        "overnight_gap_risk": True,
        "calendar_covered": acquired.year in _calendar()["covered_years"],
        "calendar_source": _calendar().get("source"),
    }
