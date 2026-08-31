#!/usr/bin/env python3
"""Update the local daily-bar cache through a bounded QFQ provider chain.

Storage and cache semantics live in ``skills.common.local_market_history``;
this script only selects symbols, fetches normalized provider rows, and orchestrates the
contract's normalize/upsert operations.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from contextlib import ExitStack, redirect_stdout
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import socket
import sys
from time import monotonic
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

from skills.common import a_share_rules
from skills.common.a_stock_http import http_get_json
from skills.common import local_market_history as history

FULL_BACKFILL_START_DATE = "1990-01-01"

#: Minimum reliable research depth.  A latest row alone is not coverage: quote
#: ingestion can seed a symbol with one current easy_tdx row.
MIN_HISTORY_TRADING_DAYS = 180

#: How far back a never-cached or shallow symbol is seeded.  The previous 240
#: calendar-day window yielded only 164 A-share sessions in production.  Four
#: hundred calendar days covers 180 sessions with room for holidays and normal
#: suspensions, without paying the ~17s/symbol full-history cost. Fetching from
#: ``FULL_BACKFILL_START_DATE`` instead costs ~17s per symbol — 24h for the
#: whole exchange — which is why that is now an explicit ``--full-backfill``
#: request rather than something an empty cache triggers by itself.
BACKFILL_CALENDAR_DAYS = 400

#: A shallow, long-listed symbol whose newest returned bar is this stale is
#: reported as suspended rather than blaming the provider for missing depth.
SUSPENSION_STALE_CALENDAR_DAYS = 30

# BaoStock's client does not set a socket timeout.  A reachable server that
# stops replying otherwise hangs in recv() beyond both our fetch budget and
# manual invocations.  Each provider socket inherits this timeout at login.
BAOSTOCK_SOCKET_TIMEOUT_SECONDS = 30.0
TENCENT_FALLBACK_WORKERS = 8

#: Fraction of the cron budget spent fetching. The rest is the margin the
#: process needs to finish its upserts and print a result instead of being
#: SIGKILLed with everything it learned still in memory.
FETCH_BUDGET_RATIO = 0.8

# Benchmarks consumed by local-only settlement. Keep this explicit: index
# symbols do not belong to the exchange equity universe and BaoStock's market
# prefix cannot be inferred from the six digits alone (000300 is Shanghai).
INDEX_BENCHMARK_SYMBOLS = {"000300": "sh.000300"}

PROVIDER_CHAIN = (
    ("baostock", "primary"),
    ("easy_tdx_qfq", "fallback"),
    ("tencent_qfqday", "fallback"),
)


def _new_source_health() -> dict[str, Any]:
    """Create the honest provider-health envelope returned by every run.

    A cache hit or non-trading day does not prove that BaoStock is healthy, so
    those paths deliberately remain ``not_checked``.  Likewise, a successful
    fallback keeps the job successful while the source health is
    ``degraded`` rather than laundering the fallback into a green primary.
    """
    return {
        "status": "not_checked",
        "degraded": False,
        "primary_provider": "baostock",
        "active_provider": None,
        "fallback_used": False,
        "providers": [
            {"provider": provider, "role": role, "status": "not_attempted"}
            for provider, role in PROVIDER_CHAIN
        ],
        "contributions": [],
        "total_rows": 0,
        "single_source": None,
        "dominant_provider": None,
        "dominant_row_ratio": None,
        "cross_source_consistency": {
            "status": "unavailable",
            "sample_size": 0,
            "reason": "not_sampled",
        },
    }


def _provider_state(source_health: dict[str, Any], provider: str) -> dict[str, Any]:
    return next(
        item for item in source_health["providers"] if item["provider"] == provider
    )


def _mark_provider(
    source_health: dict[str, Any], provider: str, status: str, reason: str | None = None
) -> None:
    item = _provider_state(source_health, provider)
    item["status"] = status
    if reason:
        item["reason"] = reason


def _record_provider_failure(
    source_health: dict[str, Any], provider: str, reason: str
) -> None:
    item = _provider_state(source_health, provider)
    if item["status"] == "ok":
        item["status"] = "partial"
    item["failed_stock_count"] = int(item.get("failed_stock_count") or 0) + 1
    item.setdefault("failure_samples", [])
    if len(item["failure_samples"]) < 3:
        item["failure_samples"].append(reason)


def _record_provider_rows(
    contribution_codes: dict[str, set[str]],
    contribution_rows: dict[str, int],
    provider: str,
    rows: Iterable[Mapping[str, Any]],
) -> None:
    """Count returned rows/codes by their actual source identity."""
    for row in rows:
        source = str(row.get("source") or provider)
        contribution_rows[source] = contribution_rows.get(source, 0) + 1
        code = _code(row.get("code"))
        if code:
            contribution_codes.setdefault(source, set()).add(code)


def _finalize_source_health(
    source_health: dict[str, Any],
    contribution_codes: Mapping[str, set[str]],
    contribution_rows: Mapping[str, int],
) -> None:
    total_rows = sum(contribution_rows.values())
    contributions = []
    for provider in sorted(
        contribution_rows,
        key=lambda item: (-contribution_rows[item], item),
    ):
        rows = contribution_rows[provider]
        contributions.append(
            {
                "provider": provider,
                "row_count": rows,
                "stock_count": len(contribution_codes.get(provider, set())),
                "row_ratio": round(rows / total_rows, 6) if total_rows else 0.0,
            }
        )
    source_health["contributions"] = contributions
    source_health["total_rows"] = total_rows
    populated = [item for item in contributions if item["row_count"] > 0]
    source_health["single_source"] = len(populated) == 1 if populated else None
    if populated:
        source_health["dominant_provider"] = populated[0]["provider"]
        source_health["dominant_row_ratio"] = populated[0]["row_ratio"]

    active_provider = source_health.get("active_provider")
    if active_provider:
        active = _provider_state(source_health, active_provider)
        if active["status"] == "partial" and not any(
            item["provider"] == active_provider for item in populated
        ):
            active["status"] = "failed"
    primary = _provider_state(source_health, "baostock")
    active = (
        _provider_state(source_health, active_provider) if active_provider else None
    )
    if primary["status"] == "ok":
        source_health["status"] = "healthy"
    elif active is not None and active["status"] in {"ok", "partial"}:
        source_health["status"] = "degraded"
        source_health["degraded"] = True
    elif any(item["status"] == "failed" for item in source_health["providers"]):
        source_health["status"] = "unavailable"

    consistency = source_health["cross_source_consistency"]
    if source_health["status"] == "degraded":
        consistency["reason"] = (
            "primary_source_unavailable"
            if source_health["fallback_used"]
            else "primary_source_fetch_errors"
        )
    elif source_health["status"] == "healthy":
        consistency["reason"] = "secondary_provider_not_sampled"
    elif source_health["status"] == "unavailable":
        consistency["reason"] = (
            "no_provider_successful_output"
            if active_provider
            else "no_provider_available"
        )


def fetch_budget_seconds() -> float | None:
    """Wall-clock fetch budget, or ``None`` when running outside cron.

    The single source of truth is the job runner's ``A_STOCK_JOB_TIMEOUT_SECONDS``
    (itself the manifest's ``run.timeout_seconds``), so the budget cannot drift
    away from the timeout that enforces it. A manual run has no budget.
    """
    raw = os.environ.get("A_STOCK_JOB_TIMEOUT_SECONDS")
    try:
        timeout = float(raw) if raw else 0.0
    except (TypeError, ValueError):
        return None
    return timeout * FETCH_BUDGET_RATIO if timeout > 0 else None


def _state_home() -> Path:
    return Path(os.environ.get("A_STOCK_STATE_HOME", "~/.hermes")).expanduser()


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.zfill(6) if text.isdigit() else ""


def _rows_from_payload(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("stocks", "data", "rows", "items", "universe"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def load_universe() -> list[str]:
    """Read the exchange universe, falling back to the existing quote cache."""
    data_dir = _state_home() / "skills" / "stock-triage" / "data"
    for filename in ("exchange_universe.json", "universe_quotes_cache.json"):
        path = data_dir / filename
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            continue
        codes = []
        for row in _rows_from_payload(payload):
            value = row.get("code") if isinstance(row, Mapping) else row
            normalized = _code(value)
            if normalized:
                codes.append(normalized)
        if codes:
            return list(dict.fromkeys([*codes, *INDEX_BENCHMARK_SYMBOLS]))
    return []


def load_universe_metadata() -> dict[str, dict[str, Any]]:
    """Best-effort listing metadata used only to explain shallow coverage."""
    path = _state_home() / "skills" / "stock-triage" / "data" / "exchange_universe.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    metadata: dict[str, dict[str, Any]] = {}
    for row in _rows_from_payload(payload):
        if not isinstance(row, Mapping):
            continue
        code = _code(row.get("code"))
        if code:
            metadata[code] = dict(row)
    return metadata


def _baostock_symbol(code: str) -> str:
    normalized = _code(code)
    if normalized in INDEX_BENCHMARK_SYMBOLS:
        return INDEX_BENCHMARK_SYMBOLS[normalized]
    return f"sh.{normalized}" if normalized.startswith("6") else f"sz.{normalized}"


def _asof(value: str | None) -> str:
    if value:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    return date.today().isoformat()


def _number(value: Any) -> float | None:
    if value in (None, "", "null", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _start_date(
    latest: str | None,
    asof: str,
    *,
    bar_count: int | None = None,
    full_backfill: bool = False,
) -> str:
    """Where this symbol's fetch starts — decided per symbol, not per run.

    A symbol with no cached bars needs a backfill window even when other
    symbols are fully cached. Deciding this from a run-wide flag meant that
    once *any* row existed, every never-fetched symbol started at ``asof`` and
    could only ever hold a single bar — a gap that never closed and never
    surfaced.
    """
    if full_backfill:
        return FULL_BACKFILL_START_DATE
    if not latest or (bar_count is not None and bar_count < MIN_HISTORY_TRADING_DAYS):
        start = datetime.strptime(asof, "%Y-%m-%d") - timedelta(days=BACKFILL_CALENDAR_DAYS)
        return start.strftime("%Y-%m-%d")
    return (datetime.strptime(latest, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def fetch_order(selected: Iterable[str], latest: Mapping[str, str]) -> list[str]:
    """Stalest symbols first, so a budget-bound run advances the frontier.

    Iterating the universe in its natural order meant a run that ran out of
    time always redid the same prefix, and the tail was never fetched at all.
    Never-cached symbols sort first: a symbol with no bars is useless to the
    consumers, while a symbol one day behind is nearly as good as current.
    """
    return sorted(selected, key=lambda code: (latest.get(code) or "", code))


def _coverage_result(
    selected: list[str],
    coverage: Mapping[str, Mapping[str, Any]],
    *,
    target_date: str,
    attempted: set[str],
    failed_codes: set[str],
    deferred: set[str],
    metadata: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarise depth without pretending IPOs or suspensions have 180 bars."""
    classifications: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    under_target = 0
    limited = 0
    remaining = 0
    desired_start = datetime.strptime(target_date, "%Y-%m-%d") - timedelta(
        days=BACKFILL_CALENDAR_DAYS
    )
    target = datetime.strptime(target_date, "%Y-%m-%d")

    for code in selected:
        item = coverage.get(code, {})
        count = int(item.get("bar_count") or 0)
        if count >= MIN_HISTORY_TRADING_DAYS:
            continue
        under_target += 1
        if code in deferred or code not in attempted:
            label = "deferred"
        elif code in failed_codes:
            label = "source_error"
        else:
            listed_raw = metadata.get(code, {}).get("listed_date")
            try:
                listed = datetime.strptime(str(listed_raw), "%Y-%m-%d")
            except (TypeError, ValueError):
                listed = None
            max_raw = item.get("max_date")
            try:
                newest = datetime.strptime(str(max_raw), "%Y-%m-%d")
            except (TypeError, ValueError):
                newest = None
            if listed is not None and listed > desired_start:
                label = "ipo"
            elif newest is not None and (target - newest).days >= SUSPENSION_STALE_CALENDAR_DAYS:
                label = "suspended"
            else:
                label = "source_insufficient"
        classifications[label] = classifications.get(label, 0) + 1
        samples.setdefault(label, [])
        if len(samples[label]) < 5:
            samples[label].append(code)
        if label in {"ipo", "suspended"}:
            limited += 1
        else:
            remaining += 1

    return {
        "target_days": MIN_HISTORY_TRADING_DAYS,
        "selected": len(selected),
        "complete": len(selected) - under_target,
        "under_target": under_target,
        "limited": limited,
        "remaining": remaining,
        "classifications": classifications,
        "samples": samples,
    }


class BaoStockSession:
    """One BaoStock login/logout session shared by the whole batch."""

    def __enter__(self) -> "BaoStockSession":
        try:
            import baostock as bs
        except ImportError as exc:
            raise RuntimeError("baostock_not_installed") from exc
        self.bs = bs
        # BaoStock prints "login success!" / "logout success!" straight to
        # stdout. This job declares expected_output=json and the runner does
        # json.loads() on the *whole* stdout, so that chatter meant the parse
        # always failed and no market snapshot was ever written. Send it to
        # stderr instead, where the logs still keep it.
        stack = ExitStack()
        stack.enter_context(redirect_stdout(sys.stderr))
        with stack:
            previous_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(BAOSTOCK_SOCKET_TIMEOUT_SECONDS)
            try:
                login = bs.login()
            finally:
                socket.setdefaulttimeout(previous_timeout)
            if str(getattr(login, "error_code", "0")) != "0":
                raise RuntimeError(f"baostock_login_failed:{getattr(login, 'error_msg', '')}")
            # Login succeeded: hand the redirect to the session so it stays in
            # place for the queries. A failure above unwinds it with the block.
            self._quiet = stack.pop_all()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self.bs.logout()
        finally:
            self._quiet.close()

    def fetch(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """Fetch and normalize one stock; callers isolate errors per stock."""
        api_code = _baostock_symbol(code)
        result = self.bs.query_history_k_data_plus(
            api_code,
            "date,open,high,low,close,preclose,volume,amount,turn,pctChg",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2",
        )
        rows = []
        while result.next():
            values = result.get_row_data()
            if not values or not values[0]:
                continue
            rows.append({
                "code": code,
                "trading_date": values[0],
                "adjust_flag": "qfq",
                "open": _number(values[1]), "high": _number(values[2]),
                "low": _number(values[3]), "close": _number(values[4]),
                "preclose": _number(values[5]), "volume": _number(values[6]),
                "amount": _number(values[7]), "turn": _number(values[8]),
                "pct_chg": _number(values[9]),
                "source": "baostock", "source_version": "query_history_k_data_plus",
            })
        return rows


class TencentSession:
    """Bounded qfq fallback when BaoStock cannot establish a session."""

    def __enter__(self) -> "TencentSession":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def fetch(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        market = "sh" if code.startswith("6") else "sz"
        # The API is count-based.  400 calendar days fit comfortably in 320
        # sessions; trim by dates after retrieval.
        query = urlencode({"param": f"{market}{code},day,,,320,qfq"})
        payload = http_get_json(
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?{query}",
            timeout=10,
        )
        stock = payload.get("data", {}).get(f"{market}{code}", {})
        raw_rows = stock.get("qfqday", []) or stock.get("day", [])
        rows: list[dict[str, Any]] = []
        previous_close: float | None = None
        for raw in raw_rows:
            try:
                trading_date = str(raw[0])
                close = float(raw[2])
                if trading_date < start_date or trading_date > end_date:
                    previous_close = close
                    continue
                pct_chg = (
                    (close - previous_close) / previous_close * 100
                    if previous_close else None
                )
                rows.append({
                    "code": code, "trading_date": trading_date,
                    "adjust_flag": "qfq", "open": float(raw[1]),
                    "close": close, "high": float(raw[3]), "low": float(raw[4]),
                    "preclose": previous_close,
                    "volume": float(raw[5]) * 100.0,  # Tencent is lots; DB is shares.
                    "amount": None, "turn": None, "pct_chg": pct_chg,
                    "source": "tencent_qfqday",
                    "source_version": "ifzq-fqkline-qfq-v1",
                })
                previous_close = close
            except (IndexError, TypeError, ValueError):
                continue
        return rows


class EasyTdxSession:
    """Shared, bounded 通达信 QFQ session; richer and faster than HTTP."""

    def __enter__(self) -> "EasyTdxSession":
        try:
            from easy_tdx import MacClient
        except ImportError as exc:
            raise RuntimeError("easy_tdx_not_installed") from exc
        self._manager = MacClient.from_best_host(timeout=10, ping_timeout=3)
        self.client = self._manager.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._manager.__exit__(*args)

    def fetch(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        from easy_tdx import Adjust, Period
        market = (
            1 if code in INDEX_BENCHMARK_SYMBOLS or code.startswith("6")
            else 2 if code.startswith(("4", "8")) else 0
        )
        frame = self.client.get_stock_kline(
            market, code, Period.DAILY, start=0, count=320, adjust=Adjust.QFQ
        )
        records = frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame or [])
        rows: list[dict[str, Any]] = []
        previous_close: float | None = None
        for raw in records:
            try:
                trading_date = str(raw.get("datetime") or raw.get("date"))[:10]
                close = float(raw["close"])
                if trading_date < start_date or trading_date > end_date:
                    previous_close = close
                    continue
                rows.append({
                    "code": code, "trading_date": trading_date, "adjust_flag": "qfq",
                    "open": float(raw["open"]), "high": float(raw["high"]),
                    "low": float(raw["low"]), "close": close,
                    "preclose": previous_close,
                    "volume": _number(raw.get("vol", raw.get("volume"))),
                    "amount": _number(raw.get("amount")), "turn": None,
                    "pct_chg": ((close - previous_close) / previous_close * 100)
                    if previous_close else None,
                    "source": "easy_tdx_qfq",
                    "source_version": "easy-tdx-mac-qfq-v1",
                })
                previous_close = close
            except (KeyError, TypeError, ValueError):
                continue
        return rows


def _fetch_from_session(
    code: str, start_date: str, end_date: str, *, session: Any,
) -> list[dict[str, Any]]:
    """Adapt a stateful fallback session to the common fetcher signature."""
    return session.fetch(code, start_date, end_date)


def fetch_baostock(
    code: str, start_date: str, end_date: str, *, session: BaoStockSession | None = None
) -> list[dict[str, Any]]:
    """Fetch one stock, optionally using the caller's shared session."""
    if session is not None:
        return session.fetch(code, start_date, end_date)
    try:
        with BaoStockSession() as batch:
            return batch.fetch(code, start_date, end_date)
    except RuntimeError:
        raise


def run(*, asof: str | None = None, codes: list[str] | None = None, dry_run: bool = False, full_backfill: bool | None = None) -> dict[str, Any]:
    target_date = _asof(asof)
    source_health = _new_source_health()
    contribution_codes: dict[str, set[str]] = {}
    contribution_rows: dict[str, int] = {}
    result: dict[str, Any] = {
        "status": "ok", "asof": target_date, "fetched": 0, "upserted": 0,
        "failed": [], "cache_stats": {}, "source_health": source_health,
    }
    try:
        trading_day = a_share_rules.is_trading_day(target_date)
    except a_share_rules.CalendarCoverageError as exc:
        result.update(status="blocked", reason=f"trading_calendar_unavailable:{exc}")
        return result
    if not trading_day:
        result.update(status="skipped", reason="non_trading_day")
        return result
    selected = list(dict.fromkeys(_code(item) for item in (codes or load_universe()) if _code(item)))
    try:
        history.ensure_schema()
        result["cache_stats"] = history.cache_stats()
    except Exception as exc:
        result.update(status="blocked", reason=f"history_cache_unavailable:{exc}")
        return result
    if not selected:
        result["status"] = "blocked"
        result["reason"] = "empty_universe"
        return result
    try:
        latest = {row["code"]: row["trading_date"] for row in history.get_latest_daily_bars(selected, target_date)}
        coverage_before = history.coverage_by_code(selected, target_date)
    except Exception as exc:
        result.update(status="blocked", reason=f"history_cache_read_failed:{exc}")
        return result
    full_backfill = bool(full_backfill)
    result["full_backfill"] = full_backfill
    if not full_backfill and all(
        latest.get(code) == target_date
        and int(coverage_before.get(code, {}).get("bar_count") or 0) >= MIN_HISTORY_TRADING_DAYS
        for code in selected
    ):
        coverage_result = _coverage_result(
            selected, coverage_before, target_date=target_date,
            attempted=set(), failed_codes=set(), deferred=set(),
            metadata=load_universe_metadata(),
        )
        result.update(
            status="skipped",
            reason="target_date_already_cached",
            budget_seconds=fetch_budget_seconds(),
            processed=0,
            remaining=0,
            budget_exhausted=False,
            coverage=coverage_result,
        )
        return result
    actionable = [
        code for code in selected
        if full_backfill
        or latest.get(code) != target_date
        or int(coverage_before.get(code, {}).get("bar_count") or 0) < MIN_HISTORY_TRADING_DAYS
    ]
    shallow = {
        code for code in actionable
        if int(coverage_before.get(code, {}).get("bar_count") or 0) < MIN_HISTORY_TRADING_DAYS
    }
    ordered = sorted(
        fetch_order(actionable, latest),
        key=lambda code: (
            0 if code in shallow else 1,
            int(coverage_before.get(code, {}).get("bar_count") or 0),
            latest.get(code) or "",
            code,
        ),
    )
    budget = fetch_budget_seconds()
    deadline = None if budget is None else monotonic() + budget
    result.update(budget_seconds=budget, processed=0, remaining=len(ordered),
                  budget_exhausted=False)
    attempted: set[str] = set()
    try:
        with ExitStack() as provider_stack:
            fetcher = fetch_baostock
            try:
                session = provider_stack.enter_context(BaoStockSession())
                result["provider"] = "baostock"
                source_health["active_provider"] = "baostock"
                _mark_provider(source_health, "baostock", "ok")
            except Exception as exc:
                result["provider_fallback_reason"] = str(exc)
                source_health["fallback_used"] = True
                _mark_provider(source_health, "baostock", "failed", str(exc))
                try:
                    session = provider_stack.enter_context(EasyTdxSession())
                    fetcher = _fetch_from_session
                    result["provider"] = "easy_tdx_qfq"
                    source_health["active_provider"] = "easy_tdx_qfq"
                    _mark_provider(source_health, "easy_tdx_qfq", "ok")
                except Exception as tdx_exc:
                    result["easy_tdx_fallback_reason"] = str(tdx_exc)
                    _mark_provider(source_health, "easy_tdx_qfq", "failed", str(tdx_exc))
                    try:
                        session = provider_stack.enter_context(TencentSession())
                        fetcher = _fetch_from_session
                        result["provider"] = "tencent_qfqday"
                        source_health["active_provider"] = "tencent_qfqday"
                        _mark_provider(source_health, "tencent_qfqday", "ok")
                    except Exception as tencent_exc:
                        _mark_provider(
                            source_health, "tencent_qfqday", "failed", str(tencent_exc)
                        )
                        raise
            if result["provider"] == "tencent_qfqday":
                pool = ThreadPoolExecutor(max_workers=TENCENT_FALLBACK_WORKERS)
                futures = {
                    pool.submit(
                        fetcher, code,
                        _start_date(
                            latest.get(code), target_date,
                            bar_count=int(coverage_before.get(code, {}).get("bar_count") or 0),
                            full_backfill=full_backfill,
                        ),
                        target_date, session=session,
                    ): code
                    for code in ordered
                }
                try:
                    timeout = None if deadline is None else max(0.0, deadline - monotonic())
                    for future in as_completed(futures, timeout=timeout):
                        code = futures[future]
                        attempted.add(code)
                        try:
                            rows = future.result()
                            _record_provider_rows(
                                contribution_codes, contribution_rows,
                                result["provider"], rows,
                            )
                            result["fetched"] += len(rows)
                            if not dry_run:
                                result["upserted"] += history.upsert_daily_bars(rows)
                        except Exception as exc:
                            result["failed"].append({"code": code, "reason": str(exc)})
                            _record_provider_failure(
                                source_health, result["provider"], str(exc)
                            )
                        result["processed"] += 1
                        result["remaining"] = len(ordered) - result["processed"]
                except FuturesTimeout:
                    result["budget_exhausted"] = True
                finally:
                    for future in futures:
                        if not future.done():
                            future.cancel()
                    pool.shutdown(wait=True, cancel_futures=True)
                if result["remaining"]:
                    result["budget_exhausted"] = True
            for index, code in enumerate(
                [] if result["provider"] == "tencent_qfqday" else ordered
            ):
                if deadline is not None and monotonic() >= deadline:
                    result["budget_exhausted"] = True
                    break
                try:
                    attempted.add(code)
                    rows = fetcher(
                        code,
                        _start_date(
                            latest.get(code), target_date,
                            bar_count=int(coverage_before.get(code, {}).get("bar_count") or 0),
                            full_backfill=full_backfill,
                        ),
                        target_date,
                        session=session,
                    )
                    _record_provider_rows(
                        contribution_codes, contribution_rows,
                        result["provider"], rows,
                    )
                    result["fetched"] += len(rows)
                    if not dry_run:
                        result["upserted"] += history.upsert_daily_bars(rows)
                except Exception as exc:  # one symbol must not abort the batch
                    result["failed"].append({"code": code, "reason": str(exc)})
                    _record_provider_failure(
                        source_health, result["provider"], str(exc)
                    )
                result["processed"] = index + 1
                result["remaining"] = len(ordered) - result["processed"]
    except Exception as exc:
        result["failed"].append({"code": "*", "reason": str(exc)})
        result.update(status="blocked", reason=str(exc))
    _finalize_source_health(source_health, contribution_codes, contribution_rows)
    result["cache_stats"] = history.cache_stats()
    coverage_after = history.coverage_by_code(selected, target_date)
    deferred = set(ordered) - attempted
    failed_codes = {item["code"] for item in result["failed"] if item.get("code") != "*"}
    result["coverage"] = _coverage_result(
        selected, coverage_after, target_date=target_date,
        attempted=attempted, failed_codes=failed_codes, deferred=deferred,
        metadata=load_universe_metadata(),
    )
    if result["status"] == "ok" and (
        result["failed"] or result["budget_exhausted"] or result["coverage"]["remaining"]
    ):
        result["status"] = "partial"
    if result["budget_exhausted"]:
        result["reason"] = (
            f"budget_exhausted:{result['remaining']} symbols deferred to the next run"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON (the cron-safe default format)")
    parser.add_argument("--asof")
    parser.add_argument("--codes", nargs="+", help="numeric codes; comma-separated values are accepted")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--full-backfill", action="store_true", help="fetch from BaoStock's full history start date")
    args = parser.parse_args(argv)
    codes = [item for value in (args.codes or []) for item in value.split(",")]
    payload = run(asof=args.asof, codes=codes or None, dry_run=args.dry_run, full_backfill=True if args.full_backfill else None)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
