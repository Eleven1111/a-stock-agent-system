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


# ---------------------------------------------------------------------------
# 按日期取制度（rule_version）
# ---------------------------------------------------------------------------
# 回测禁止用「今天的规则」定价历史涨跌停：2020 年的创业板是 10cm 不是 20cm。
# 下面每条断点都写明生效日与出处，新增断点必须同时补一条断点单测。
RULE_VERSION_SCHEMA = "a_share_price_limit_rule_v1"

# 创业板注册制改革，涨跌幅 10% → 20%（首批注册制企业 2020-08-24 上市当日起）。
CHINEXT_20PCT_FROM = date(2020, 8, 24)
# 科创板开市，自始适用 20%。
STAR_MARKET_OPEN = date(2019, 7, 22)
# 北交所开市，自始适用 30%。此前的新三板精选层不在本函数覆盖范围内。
BSE_OPEN = date(2021, 11, 15)
# 沪市主板风险警示股票涨跌幅 5% → 10%。
# 出处：docs_private/hot-money-emotion-system-upgrade-plan-2026-08.md §8.1(b)。
# 该文只声明了沪市主板，深市主板未见同口径表述，故此处只对沪市改判，
# 深市主板风险警示沿用既有 5%（保持既有调用方行为不变），差异见交付报告。
SSE_RISK_WARNING_10PCT_FROM = date(2026, 7, 6)
# 注册制板块新股上市后前 N 个交易日不设涨跌幅限制。
REGISTRATION_NO_LIMIT_SESSIONS = 5


def board_of(code: str) -> str | None:
    """代码 → 板块标识；无法判定返回 None（调用方 fail-closed）。"""
    normalized = str(code).strip().lower()
    if normalized.startswith(("sh", "sz", "bj")):
        normalized = normalized[2:]
    if not normalized.isdigit() or len(normalized) != 6:
        return None
    if normalized.startswith("688") or normalized.startswith("689"):
        return "star"
    if normalized.startswith(("300", "301")):
        return "chinext"
    if normalized.startswith(("4", "8", "920")):
        return "bse"
    if normalized.startswith("60"):
        return "sse_main"
    if normalized.startswith("00"):
        return "szse_main"
    return None


def exchange_of(code: str) -> str | None:
    """代码 → 交易所标识（SSE / SZSE / BSE）。"""
    board = board_of(code)
    if board in {"star", "sse_main"}:
        return "SSE"
    if board in {"chinext", "szse_main"}:
        return "SZSE"
    if board == "bse":
        return "BSE"
    return None


def _board_limit_on(board: str, day: date, *, is_st: bool) -> tuple[float | None, str]:
    """某板块在某日的常规涨跌幅（不含新股特殊期）。返回 (limit_pct, rule_id)。"""
    if board == "star":
        if day < STAR_MARKET_OPEN:
            return None, "star_before_open"
        return 20.0, "star_20pct"
    if board == "chinext":
        if day < CHINEXT_20PCT_FROM:
            # 风险警示股票在 10cm 时代同样按主板 ST 口径 5%。
            return (5.0, "chinext_st_5pct") if is_st else (10.0, "chinext_10pct")
        return 20.0, "chinext_20pct"
    if board == "bse":
        if day < BSE_OPEN:
            return None, "bse_before_open"
        return 30.0, "bse_30pct"
    if board == "sse_main":
        if is_st:
            if day >= SSE_RISK_WARNING_10PCT_FROM:
                return 10.0, "sse_main_risk_warning_10pct"
            return 5.0, "sse_main_risk_warning_5pct"
        return 10.0, "sse_main_10pct"
    if board == "szse_main":
        if is_st:
            return 5.0, "szse_main_risk_warning_5pct"
        return 10.0, "szse_main_10pct"
    return None, "board_unknown"


def price_limit_rule(
    *,
    code: str,
    asof: date | datetime | str | None,
    is_st: bool = False,
    listing_stage: str = "normal",
    sessions_since_listing: int | None = None,
) -> dict[str, Any]:
    """按 (日期, 交易所, 板块, 风险警示) 返回当时有效的涨跌幅制度。

    ``limit_pct is None`` 且 ``status == "known"`` 表示当日不设涨跌幅限制
    （注册制新股特殊期）；``status == "blocked"`` 表示制度未知，调用方必须
    保守拒绝而不是回退到今天的规则。
    ``sessions_since_listing`` 为上市后第几个交易日（上市首日=1），缺省 None
    表示调用方未提供，此时不做新股特殊期判定。
    """
    if asof is None or listing_stage not in {"normal", "initial_no_limit"}:
        return {"schema": RULE_VERSION_SCHEMA, "status": "blocked",
                "reason": "rule_unknown", "limit_pct": None}
    try:
        day = _as_date(asof)
    except ValueError:
        return {"schema": RULE_VERSION_SCHEMA, "status": "blocked",
                "reason": "rule_unknown", "limit_pct": None}
    board = board_of(code)
    if board is None:
        return {"schema": RULE_VERSION_SCHEMA, "status": "blocked",
                "reason": "rule_unknown", "limit_pct": None}

    base_pct, rule_id = _board_limit_on(board, day, is_st=bool(is_st))
    registration_board = board == "star" or (
        board == "chinext" and day >= CHINEXT_20PCT_FROM
    ) or (board == "bse" and day >= BSE_OPEN)
    in_new_share_window = listing_stage == "initial_no_limit" or (
        registration_board
        and sessions_since_listing is not None
        and 1 <= int(sessions_since_listing) <= REGISTRATION_NO_LIMIT_SESSIONS
    )
    if base_pct is None and not in_new_share_window:
        return {"schema": RULE_VERSION_SCHEMA, "status": "blocked",
                "reason": rule_id, "limit_pct": None}
    if in_new_share_window:
        return {
            "schema": RULE_VERSION_SCHEMA,
            "status": "known",
            "reason": "initial_listing_no_daily_limit",
            "rule_id": "registration_new_share_no_limit",
            "limit_pct": None,
            "board": board,
            "exchange": exchange_of(code),
            "asof": day.isoformat(),
            "is_st": bool(is_st),
        }
    return {
        "schema": RULE_VERSION_SCHEMA,
        "status": "known",
        "reason": "rule_resolved",
        "rule_id": rule_id,
        "limit_pct": base_pct,
        "board": board,
        "exchange": exchange_of(code),
        "asof": day.isoformat(),
        "is_st": bool(is_st),
    }


def price_limit_pct_on(
    code: str,
    asof: date | datetime | str | None,
    *,
    is_st: bool = False,
    sessions_since_listing: int | None = None,
) -> float | None:
    """便捷取值：当日涨跌幅百分比。制度未知或不设限均返回 None（fail-closed）。"""
    rule = price_limit_rule(
        code=code, asof=asof, is_st=is_st,
        sessions_since_listing=sessions_since_listing,
    )
    return rule["limit_pct"] if rule["status"] == "known" else None


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
    # 按日期取制度：历史回测不得套用今天的涨跌幅（见 price_limit_rule 断点表）。
    rule = price_limit_rule(code=code, asof=decision_day, is_st=is_st)
    if rule["status"] != "known" or rule["limit_pct"] is None:
        return {"status": "blocked", "reason": "rule_unknown", "limit_pct": None}
    return {
        "status": "known",
        "reason": "rule_resolved",
        "limit_pct": rule["limit_pct"],
        "rule_id": rule["rule_id"],
        "direction": direction,
        "listing_stage": listing_stage,
        "listing_date": listed_on.isoformat(),
        "is_st": is_st,
    }
