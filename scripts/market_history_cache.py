#!/usr/bin/env python3
"""Update the local daily-bar cache from BaoStock.

Storage and cache semantics live in ``skills.common.local_market_history``;
this script only selects symbols, fetches BaoStock rows, and orchestrates the
contract's normalize/upsert operations.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sys
from time import monotonic
from typing import Any, Iterable, Mapping

from skills.common import a_share_rules
from skills.common import local_market_history as history

FULL_BACKFILL_START_DATE = "1990-01-01"

#: How far back a never-cached symbol is seeded. The cache is a fallback for
#: ``fetch_tencent_kline``, which only uses it when it holds at least the
#: requested lookback; the deepest caller asks for 120 trading days, so ~240
#: calendar days covers every consumer with margin. Fetching from
#: ``FULL_BACKFILL_START_DATE`` instead costs ~17s per symbol — 24h for the
#: whole exchange — which is why that is now an explicit ``--full-backfill``
#: request rather than something an empty cache triggers by itself.
BACKFILL_CALENDAR_DAYS = 240

#: Fraction of the cron budget spent fetching. The rest is the margin the
#: process needs to finish its upserts and print a result instead of being
#: SIGKILLed with everything it learned still in memory.
FETCH_BUDGET_RATIO = 0.8

# Benchmarks consumed by local-only settlement. Keep this explicit: index
# symbols do not belong to the exchange equity universe and BaoStock's market
# prefix cannot be inferred from the six digits alone (000300 is Shanghai).
INDEX_BENCHMARK_SYMBOLS = {"000300": "sh.000300"}


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


def _start_date(latest: str | None, asof: str, *, full_backfill: bool = False) -> str:
    """Where this symbol's fetch starts — decided per symbol, not per run.

    A symbol with no cached bars needs a backfill window even when other
    symbols are fully cached. Deciding this from a run-wide flag meant that
    once *any* row existed, every never-fetched symbol started at ``asof`` and
    could only ever hold a single bar — a gap that never closed and never
    surfaced.
    """
    if full_backfill:
        return FULL_BACKFILL_START_DATE
    if not latest:
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
            login = bs.login()
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
    result: dict[str, Any] = {"status": "ok", "asof": target_date, "fetched": 0, "upserted": 0, "failed": [], "cache_stats": {}}
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
    except Exception as exc:
        result.update(status="blocked", reason=f"history_cache_read_failed:{exc}")
        return result
    full_backfill = bool(full_backfill)
    result["full_backfill"] = full_backfill
    if not full_backfill and all(latest.get(code) == target_date for code in selected):
        result.update(
            status="skipped",
            reason="target_date_already_cached",
            budget_seconds=fetch_budget_seconds(),
            processed=0,
            remaining=0,
            budget_exhausted=False,
        )
        return result
    ordered = fetch_order(selected, latest)
    budget = fetch_budget_seconds()
    deadline = None if budget is None else monotonic() + budget
    result.update(budget_seconds=budget, processed=0, remaining=len(ordered),
                  budget_exhausted=False)
    try:
        with BaoStockSession() as session:
            for index, code in enumerate(ordered):
                if deadline is not None and monotonic() >= deadline:
                    result["budget_exhausted"] = True
                    break
                try:
                    rows = fetch_baostock(
                        code,
                        _start_date(latest.get(code), target_date, full_backfill=full_backfill),
                        target_date,
                        session=session,
                    )
                    result["fetched"] += len(rows)
                    if not dry_run:
                        result["upserted"] += history.upsert_daily_bars(rows)
                except Exception as exc:  # one symbol must not abort the batch
                    result["failed"].append({"code": code, "reason": str(exc)})
                result["processed"] = index + 1
                result["remaining"] = len(ordered) - result["processed"]
    except RuntimeError as exc:
        result["failed"].append({"code": "*", "reason": str(exc)})
        result.update(status="blocked", reason=str(exc))
    result["cache_stats"] = history.cache_stats()
    if result["status"] == "ok" and (result["failed"] or result["budget_exhausted"]):
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
