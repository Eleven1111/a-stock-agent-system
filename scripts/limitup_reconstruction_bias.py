#!/usr/bin/env python3
"""Compare real limit-up metadata with approximate BaoStock 5-minute states.

This is a research audit, not an event backfill. The artifact always records
that reconstructed rows are ineligible for ``divergence_reseal`` because
intrabar open-and-reseal sequences are structurally unobservable at 5-minute
resolution.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence

import skills.common  # noqa: F401,E402 -- puts skills/common on sys.path
import limitup_event_reconstruction as reconstruction  # noqa: E402
import local_market_history  # noqa: E402
import minute_rows_source  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import atomic_write_json  # noqa: E402


TruthFetcher = Callable[[str, str], tuple[list[dict[str, Any]], dict[str, Any]]]
DailyLoader = Callable[[Sequence[str], str, int], list[dict[str, Any]]]
MinuteCollector = Callable[..., tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[str, Any]]]


def _normalise_date(value: Any) -> str:
    text = str(value or "").strip().replace("-", "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return str(value or "")


def _seal_time(value: Any) -> str | None:
    if value in (None, "", "-"):
        return None
    try:
        text = str(int(float(value)))
    except (TypeError, ValueError):
        text = str(value).replace(":", "").strip()
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def standardize_truth_row(row: Mapping[str, Any], trading_date: str) -> dict[str, Any]:
    open_count = _number(row.get("炸板次数"))
    last_seal = _seal_time(row.get("最后封板时间"))
    return {
        "date": _normalise_date(trading_date),
        "code": str(row.get("代码") or "").zfill(6),
        "name": str(row.get("名称") or ""),
        "first_seal_time": _seal_time(row.get("首次封板时间")),
        "last_seal_time": last_seal,
        "reseal_time": last_seal if open_count is not None and open_count > 0 else None,
        "open_board_count": open_count,
        "event_source": "eastmoney_zt_pool",
    }


def fetch_truth_events(start: str, end: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch zt_pool only for locally known trading days; empty days stay visible."""
    import akshare as ak

    dates = local_market_history.trading_dates_between(start, end)
    events: list[dict[str, Any]] = []
    missing_dates: list[str] = []
    counts: dict[str, int] = {}
    for trading_date in dates:
        frame = ak.stock_zt_pool_em(date=trading_date.replace("-", ""))
        if frame is None or len(frame) == 0:
            missing_dates.append(trading_date)
            counts[trading_date] = 0
            continue
        records = [standardize_truth_row(row, trading_date) for row in frame.to_dict("records")]
        records = [row for row in records if row["code"] != "000000"]
        events.extend(records)
        counts[trading_date] = len(records)
        time.sleep(0.05)
    return events, {
        "source": "eastmoney_zt_pool",
        "requested_trading_dates": len(dates),
        "covered_trading_dates": len(dates) - len(missing_dates),
        "missing_dates": missing_dates,
        "event_counts": counts,
        "status": "ok" if dates and not missing_dates else "blocked",
    }


def _daily_rows(codes: Sequence[str], end: str, lookback: int) -> list[dict[str, Any]]:
    return local_market_history.get_daily_bars(codes, end, lookback, adjust_flag="qfq")


def _default_out(start: str, end: str) -> str:
    slug = f"{start.replace('-', '')}_{end.replace('-', '')}"
    return data_file("chanlun-backtest", f"limitup_reconstruction_bias_{slug}.json")


def run(
    start: str,
    end: str,
    *,
    out: str | None = None,
    truth_fetcher: TruthFetcher = fetch_truth_events,
    daily_loader: DailyLoader = _daily_rows,
    minute_collector: MinuteCollector = minute_rows_source.collect,
) -> dict[str, Any]:
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    if start_day > end_day:
        raise ValueError("start must not be after end")

    truth, truth_diagnostics = truth_fetcher(start, end)
    codes = sorted({str(row.get("code") or "").zfill(6) for row in truth})
    lookback = max(10, (end_day - start_day).days + 10)
    daily = daily_loader(codes, end, lookback) if codes else []
    daily_by_key = {
        (str(row.get("trading_date") or row.get("date") or ""), str(row.get("code") or "").zfill(6)): row
        for row in daily
    }
    minute_by_key, minute_diagnostics = minute_collector(
        truth, mode=minute_rows_source.MODE_BAOSTOCK
    )

    inferred: dict[tuple[str, str], dict[str, Any]] = {}
    missing_daily = 0
    minute_close_fallbacks = 0
    missing_limit_price = 0
    for event in truth:
        key = (str(event["date"]), str(event["code"]).zfill(6))
        daily_bar = daily_by_key.get(key)
        minute_rows = minute_by_key.get(key) or []
        limit_price = _number(daily_bar.get("close")) if daily_bar else None
        if limit_price is None:
            missing_daily += 1
            limit_price = _number(minute_rows[-1].get("close")) if minute_rows else None
            if limit_price is not None:
                minute_close_fallbacks += 1
        if limit_price is None:
            missing_limit_price += 1
            continue
        inferred[key] = reconstruction.infer_5m_close_state(
            minute_rows, limit_price=limit_price
        )

    report = reconstruction.build_bias_report(truth, inferred)
    if truth_diagnostics.get("status") != "ok":
        report["status"] = "blocked"
    report.update({
        "start": start,
        "end": end,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "truth_diagnostics": truth_diagnostics,
        "minute_diagnostics": minute_diagnostics,
        "missing_daily_events": missing_daily,
        "minute_close_limit_price_fallbacks": minute_close_fallbacks,
        "missing_limit_price_events": missing_limit_price,
        "artifact_role": "bias_audit_only_not_event_backfill",
    })
    target = str(Path(out or _default_out(start, end)).expanduser())
    atomic_write_json(target, report)
    report["artifact"] = target
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--out")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    end = args.end or date.today().isoformat()
    start = args.start or (date.fromisoformat(end) - timedelta(days=21)).isoformat()
    payload = run(start, end, out=args.out)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True if args.json else False,
                     indent=None if args.json else 2))
    return 0 if payload.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
