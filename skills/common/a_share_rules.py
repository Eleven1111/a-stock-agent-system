"""A-share trading calendar and T+1 execution constraints."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

from config_registry import config_path


CALENDAR_FILE = str(config_path("calendar"))


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


def resolve_price_limit_rule(
    *,
    code: str,
    asof: date | datetime | str | None,
    listing_date: date | datetime | str | None,
    listing_stage: str | None,
    is_st: bool | None,
    direction: str | None,
) -> dict[str, Any]:
    """Resolve a price-limit rule without inferring missing security state."""
    if (
        asof is None
        or listing_date is None
        or listing_stage not in {"normal", "initial_no_limit"}
        or not isinstance(is_st, bool)
        or direction not in {"buy", "sell"}
    ):
        return {"status": "blocked", "reason": "rule_unknown", "limit_pct": None}
    try:
        decision_day = _as_date(asof)
        listed_on = _as_date(listing_date)
    except ValueError:
        return {"status": "blocked", "reason": "rule_unknown", "limit_pct": None}
    if listed_on > decision_day:
        return {"status": "blocked", "reason": "rule_unknown", "limit_pct": None}
    if listing_stage == "initial_no_limit":
        return {
            "status": "known",
            "reason": "initial_listing_no_daily_limit",
            "limit_pct": None,
            "direction": direction,
        }
    normalized = str(code).strip().lower()
    if normalized.startswith(("sh", "sz", "bj")):
        normalized = normalized[2:]
    if not normalized.isdigit() or len(normalized) != 6:
        return {"status": "blocked", "reason": "rule_unknown", "limit_pct": None}
    # 创业板/科创板风险警示股票仍适用板块 20% 涨跌幅；5% 仅适用于主板 ST。
    if normalized.startswith(("300", "301", "688")):
        limit = 20.0
    elif is_st:
        limit = 5.0
    elif normalized.startswith(("4", "8", "920")):
        limit = 30.0
    elif normalized.startswith(("00", "60", "601", "603", "605")):
        limit = 10.0
    else:
        return {"status": "blocked", "reason": "rule_unknown", "limit_pct": None}
    return {
        "status": "known",
        "reason": "rule_resolved",
        "limit_pct": limit,
        "direction": direction,
        "listing_stage": listing_stage,
        "listing_date": listed_on.isoformat(),
        "is_st": is_st,
    }
