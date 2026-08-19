#!/usr/bin/env python3
"""Update the local daily-bar cache from BaoStock.

Storage and cache semantics live in ``skills.common.local_market_history``;
this script only selects symbols, fetches BaoStock rows, and orchestrates the
contract's normalize/upsert operations.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.common import local_market_history as history  # noqa: E402

FULL_BACKFILL_START_DATE = "1990-01-01"


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
            return list(dict.fromkeys(codes))
    return []


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
    if full_backfill:
        return FULL_BACKFILL_START_DATE
    if not latest:
        return asof
    return (datetime.strptime(latest, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


class BaoStockSession:
    """One BaoStock login/logout session shared by the whole batch."""

    def __enter__(self) -> "BaoStockSession":
        try:
            import baostock as bs
        except ImportError as exc:
            raise RuntimeError("baostock_not_installed") from exc
        self.bs = bs
        login = bs.login()
        if str(getattr(login, "error_code", "0")) != "0":
            raise RuntimeError(f"baostock_login_failed:{getattr(login, 'error_msg', '')}")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.bs.logout()

    def fetch(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """Fetch and normalize one stock; callers isolate errors per stock."""
        api_code = f"sh.{code}" if code.startswith("6") else f"sz.{code}"
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
    selected = list(dict.fromkeys(_code(item) for item in (codes or load_universe()) if _code(item)))
    result: dict[str, Any] = {"status": "ok", "asof": target_date, "fetched": 0, "upserted": 0, "failed": [], "cache_stats": {}}
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
    if full_backfill is None:
        full_backfill = result["cache_stats"].get("row_count", 0) == 0
    result["full_backfill"] = bool(full_backfill)
    try:
        with BaoStockSession() as session:
            for code in selected:
                try:
                    rows = fetch_baostock(
                        code,
                        _start_date(latest.get(code), target_date, full_backfill=bool(full_backfill)),
                        target_date,
                        session=session,
                    )
                    result["fetched"] += len(rows)
                    if not dry_run:
                        result["upserted"] += history.upsert_daily_bars(rows)
                except Exception as exc:  # one symbol must not abort the batch
                    result["failed"].append({"code": code, "reason": str(exc)})
    except RuntimeError as exc:
        result["failed"].append({"code": "*", "reason": str(exc)})
        result.update(status="blocked", reason=str(exc))
    result["cache_stats"] = history.cache_stats()
    if result["failed"] and result["status"] == "ok":
        result["status"] = "partial"
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
