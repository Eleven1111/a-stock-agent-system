"""统一的真实集合竞价与前一交易日量能适配器。

本模块是 auction collector 的唯一数据入口：

* 集合竞价量价：``easy_tdx.MacClient.get_auction``，协议命令为 ``0x123D``；
* 五档盘口：腾讯实时快照，仅合并 ``bids``/``asks``，不采用其成交量；
* 昨日量额：优先读取本地历史库，缺失后使用 ``mootdx_adapter``，最后使用腾讯历史 K 线；
* 任一竞价关键字段缺失都返回 ``blocked``；五档失败单独降级并保留原因。

easy_tdx 的 ``matched``/``unmatched`` 原始单位是股；现有竞价金额公式使用手，
所以这里保留原始股数字段，同时把 ``matched / 100`` 作为 ``volume``（手）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time
from math import isfinite
import threading
from time import monotonic
from typing import Any, Callable, Iterable, Mapping

from market_adapters import fetch_tencent_kline
from a_stock_http import fetch_tencent_snapshot, tencent_symbol
from mootdx_adapter import fetch_mootdx_bars
import local_market_history


PROVIDER = "easy_tdx_mac_0x123d"
AUCTION_START = time(9, 15)
AUCTION_END = time(9, 25)
PREVIOUS_DAY_WORKERS = 12
TENCENT_BOOK_BATCH_SIZE = 80
_EASY_TDX_KLINE_LOCK = threading.Lock()


def _budget_reason(deadline_seconds: float) -> str:
    return (
        f"budget_exhausted: 竞价抓取超出 {deadline_seconds:g}s 预算，该标的未取数"
    )


def _bare_code(code: Any) -> str:
    text = str(code or "").strip().lower()
    if text[:2] in {"sh", "sz", "bj"}:
        text = text[2:]
    return text.split(".", 1)[0].zfill(6) if text else ""


def _market(code: str) -> int:
    return 1 if _bare_code(code).startswith("6") else 0


def _time_value(value: Any) -> time | None:
    if isinstance(value, time):
        return value
    if isinstance(value, datetime):
        return value.time()
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text[:8], fmt).time()
        except ValueError:
            continue
    return None


def _records(raw: Any) -> Iterable[Mapping[str, Any]]:
    if raw is None:
        return []
    if hasattr(raw, "to_dict"):
        try:
            return raw.to_dict("records")
        except (TypeError, ValueError):
            return []
    if isinstance(raw, Mapping):
        return [raw]
    return [row for row in raw if isinstance(row, Mapping)]


def _number(value: Any, *, integer: bool = False) -> float | int | None:
    if isinstance(value, bool) or value in (None, "", "-"):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(parsed) or parsed < 0:
        return None
    return int(parsed) if integer else parsed


def normalize_auction_rows(raw: Any) -> list[dict[str, Any]]:
    """Normalize easy_tdx rows, filtering to 09:15-09:25 and sorting ascending."""
    normalized: list[dict[str, Any]] = []
    for row in _records(raw):
        clock = _time_value(row.get("time") or row.get("t"))
        price = _number(row.get("price"))
        matched = _number(row.get("matched"), integer=True)
        unmatched = _number(row.get("unmatched"), integer=True)
        if (
            clock is None
            or not (AUCTION_START <= clock <= AUCTION_END)
            or price is None
            or price <= 0
            or matched is None
            or unmatched is None
        ):
            continue
        normalized.append({
            "t": clock.strftime("%H:%M:%S"),
            "price": float(price),
            # easy_tdx / 0x123D contract: both are shares, not lots.
            "matched": int(matched),
            "unmatched": int(unmatched),
            "matched_unit": "share",
            "unmatched_unit": "share",
            "volume": round(int(matched) / 100.0, 4),
            "volume_unit": "lot",
            "provider": PROVIDER,
        })
    return sorted(normalized, key=lambda item: item["t"])


def _fetch_easy_tdx_with_client(client: Any, code: str) -> list[dict[str, Any]]:
    return normalize_auction_rows(client.get_auction(_market(code), _bare_code(code)))


def _has_valid_book(snapshot: Mapping[str, Any]) -> bool:
    def has_level(levels: Any) -> bool:
        return any(
            isinstance(level, (tuple, list))
            and len(level) >= 2
            and (level[0] is not None or (level[1] or 0) > 0)
            for level in (levels or [])
        )

    return has_level(snapshot.get("bids")) and has_level(snapshot.get("asks"))


def _book_fields(snapshot: Mapping[str, Any] | None, reason: str | None = None) -> dict[str, Any]:
    if snapshot is not None and _has_valid_book(snapshot):
        fetched_at = snapshot.get("fetched_at")
        return {
            "bids": list(snapshot.get("bids") or []),
            "asks": list(snapshot.get("asks") or []),
            "book_provider": "tencent",
            "book_status": "ok",
            "book_failure_reason": None,
            "book_provenance": {
                "provider": "tencent",
                "provider_version": snapshot.get("provider_version"),
                "fetched_at": fetched_at,
            },
            "book_observation_provenance": {
                "observation_kind": "observed",
                "observed_at": fetched_at,
                "provider": "tencent",
            },
            "book_is_imputed": False,
        }
    return {
        "bids": [],
        "asks": [],
        "book_provider": "tencent",
        "book_status": "unavailable",
        "book_failure_reason": reason or "腾讯实时快照缺少有效五档盘口",
        "book_provenance": {"provider": "tencent"},
        "book_observation_provenance": {
            "observation_kind": "unavailable",
            "provider": "tencent",
        },
        "book_is_imputed": None,
    }


def _fetch_tencent_books(
    codes: Iterable[str],
    *,
    budget_exhausted: Callable[[], bool] | None = None,
    deadline_seconds: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch books in bounded batches and return one auditable result per code."""
    normalized = list(dict.fromkeys(_bare_code(code) for code in codes if _bare_code(code)))
    books: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(normalized), TENCENT_BOOK_BATCH_SIZE):
        batch = normalized[offset:offset + TENCENT_BOOK_BATCH_SIZE]
        if budget_exhausted is not None and budget_exhausted():
            reason = _budget_reason(deadline_seconds)
            books.update({code: _book_fields(None, reason) for code in batch})
            continue
        symbols = [tencent_symbol(code) for code in batch]
        try:
            raw_books = fetch_tencent_snapshot(symbols)
        except Exception as exc:
            reason = f"腾讯五档不可用: {type(exc).__name__}: {exc}"
            books.update({code: _book_fields(None, reason) for code in batch})
            continue
        by_code = {
            _bare_code(raw_code): snapshot
            for raw_code, snapshot in raw_books.items()
            if isinstance(snapshot, Mapping)
        }
        for code in batch:
            books[code] = _book_fields(by_code.get(code))
    return books


def _merge_book_into_rows(
    rows: Iterable[Mapping[str, Any]], book: Mapping[str, Any],
) -> list[dict[str, Any]]:
    # Deliberately whitelist book fields: Tencent price/volume must never
    # replace the easy_tdx auction contract or previous-day metrics.
    fields = {
        key: book.get(key)
        for key in (
            "bids", "asks", "book_provider", "book_status",
            "book_failure_reason", "book_provenance",
            "book_observation_provenance", "book_is_imputed",
        )
    }
    return [{**dict(row), **fields} for row in rows]


def fetch_easy_tdx_previous_day_metrics(
    client: Any,
    code: str,
    *,
    asof: date | str | None = None,
) -> dict[str, Any]:
    """Read and cache the latest easy_tdx daily bar before ``asof``.

    The auction and daily-K-line calls share one TDX connection.  This avoids
    depending on the flaky mootdx TCP history endpoint during the 09:15 window.
    """
    if isinstance(asof, datetime):
        event_day = asof.date()
    elif isinstance(asof, date):
        event_day = asof
    elif asof:
        event_day = date.fromisoformat(str(asof)[:10])
    else:
        event_day = date.today()
    try:
        from easy_tdx import Period

        # The auction client is shared by the batch collector; serialize K-line
        # calls because the underlying socket client is not guaranteed to be
        # thread-safe while previous-day fallback work runs in a pool.
        with _EASY_TDX_KLINE_LOCK:
            raw = client.get_stock_kline(
                _market(code), _bare_code(code), Period.DAILY, start=0, count=10,
            )
    except Exception:
        return {}
    bars = []
    for row in _records(raw):
        if not isinstance(row, Mapping):
            continue
        bar_date = str(row.get("datetime") or row.get("date") or "")[:10]
        volume = _number(row.get("vol") if row.get("vol") is not None else row.get("volume"))
        amount = _number(row.get("amount"))
        close = _number(row.get("close"))
        if not bar_date or volume is None or volume <= 0:
            continue
        try:
            parsed_date = date.fromisoformat(bar_date)
        except ValueError:
            continue
        if parsed_date < event_day:
            bars.append({
                "date": parsed_date,
                "volume": float(volume),
                "amount": float(amount or 0),
                "close": float(close) if close is not None else None,
            })
    latest = max(bars, key=lambda item: item["date"], default=None)
    if latest is None:
        return {}
    local_market_history.upsert_daily_bars([{
        "code": _bare_code(code),
        "trading_date": latest["date"].isoformat(),
        "volume": latest["volume"],
        "amount": latest["amount"],
        "source": "easy_tdx_daily",
        "source_version": "easy-tdx-daily-v1",
    }])
    return _previous_day_result(
        latest, provider="easy_tdx_daily", dataset="daily_bars"
    )


def fetch_easy_tdx_auction(code: str) -> list[dict[str, Any]]:
    """Fetch one real auction series.  Import is lazy so fixture/CI stays optional."""
    try:
        from easy_tdx import MacClient
    except ImportError:
        return []
    try:
        with MacClient.from_best_host() as client:
            return _fetch_easy_tdx_with_client(client, code)
    except Exception:
        return []


def _parse_previous_bar(bar: Mapping[str, Any], event_day: date) -> dict[str, Any] | None:
    try:
        bar_date = date.fromisoformat(str(bar.get("date"))[:10])
        volume = float(bar.get("volume"))
    except (AttributeError, TypeError, ValueError):
        return None
    if bar_date >= event_day or not isfinite(volume) or volume <= 0:
        return None
    try:
        amount = float(bar.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return {
        "date": bar_date,
        "volume": volume,
        "amount": amount if isfinite(amount) and amount > 0 else 0.0,
    }


def _previous_day_result(
    bar: Mapping[str, Any], *, provider: str, dataset: str,
) -> dict[str, Any]:
    bar_date = bar["date"]
    result = {
        "prev_day_volume": bar["volume"],
        "prev_day_amount": bar["amount"] if bar["amount"] > 0 else None,
        "prev_day_date": bar_date.isoformat(),
        "prev_day_provider": provider,
        "prev_day_provenance": {
            "provider": provider,
            "dataset": dataset,
            "date": bar_date.isoformat(),
        },
    }
    if provider == "local_history":
        source_version = str(bar.get("source_version") or "local-history-v1")
        result["prev_day_source_version"] = source_version
        result["prev_day_provenance"]["source_version"] = source_version
    if bar.get("close") is not None:
        result["prev_close"] = bar["close"]
    return result


def _latest_eligible_previous_bar(
    bars: Iterable[Mapping[str, Any]] | None, event_day: date,
) -> dict[str, Any] | None:
    eligible = [
        parsed
        for bar in bars or []
        if isinstance(bar, Mapping)
        and (parsed := _parse_previous_bar(bar, event_day)) is not None
    ]
    return max(eligible, key=lambda item: item["date"]) if eligible else None


def fetch_previous_day_metrics(
    code: str,
    *,
    asof: date | str | None = None,
    easy_tdx_client: Any | None = None,
) -> dict[str, Any]:
    """Read the latest valid daily bar strictly before the event date.

    Tencent is queried through the existing historical K-line adapter only.  In
    particular, its real-time five-level ``volume`` is never treated as a
    previous-day value.
    """
    if isinstance(asof, datetime):
        event_day = asof.date()
    elif isinstance(asof, date):
        event_day = asof
    elif asof:
        event_day = date.fromisoformat(str(asof)[:10])
    else:
        event_day = date.today()

    try:
        local_bars = local_market_history.get_latest_daily_bars(
            [_bare_code(code)], event_day.isoformat()
        )
    except Exception:
        local_bars = []
    local_bars = [
        {**row, "date": row.get("date") or row.get("trading_date")}
        for row in local_bars
        if isinstance(row, Mapping)
    ]
    latest = _latest_eligible_previous_bar(local_bars, event_day)
    if latest is not None:
        source_row = next(
            (
                row for row in local_bars
                if str(row.get("trading_date") or row.get("date"))[:10]
                == latest["date"].isoformat()
            ),
            {},
        )
        latest = {**latest, "source_version": source_row.get("source_version")}
        return _previous_day_result(
            latest, provider="local_history", dataset="daily_bars"
        )

    if easy_tdx_client is not None:
        latest = fetch_easy_tdx_previous_day_metrics(
            easy_tdx_client, code, asof=event_day
        )
        if latest:
            return latest

    try:
        mootdx_bars = fetch_mootdx_bars(_bare_code(code), days=10)
    except Exception:
        mootdx_bars = []
    latest = _latest_eligible_previous_bar(mootdx_bars, event_day)
    if latest is not None:
        return _previous_day_result(latest, provider="mootdx", dataset="daily_bars")

    # This is deliberately the historical Tencent K-line API, not the
    # real-time quote/five-level API whose volume is an intraday cumulative.
    market = "sh" if _bare_code(code).startswith("6") else "sz"
    try:
        tencent_bars = fetch_tencent_kline(
            _bare_code(code), market=market, days=10, ktype="day",
        )
    except Exception:
        tencent_bars = []
    latest = _latest_eligible_previous_bar(tencent_bars, event_day)
    if latest is not None:
        return _previous_day_result(
            latest, provider="tencent_kline", dataset="daily_kline",
        )
    return {}


def fetch_real_auction_observation(
    code: str,
    *,
    asof: date | str | None = None,
) -> dict[str, Any]:
    """Return one auditable observation or a fail-closed reason."""
    rows = fetch_easy_tdx_auction(code)
    if not rows:
        return {
            "status": "blocked",
            "provider": PROVIDER,
            "reason": "easy_tdx 0x123D 无有效 09:15-09:25 竞价数据",
            "snapshots": [],
        }
    previous = fetch_previous_day_metrics(code, asof=asof)
    prev_day_volume = _number(previous.get("prev_day_volume"))
    if prev_day_volume is None or prev_day_volume <= 0:
        return {
            "status": "blocked",
            "provider": f"{PROVIDER}+previous_day_volume",
            "reason": "prev_day_volume 缺失或无效（mootdx 与腾讯历史 K 线均失败）",
            "snapshots": [],
        }
    book = _fetch_tencent_books([code]).get(_bare_code(code), _book_fields(None))
    snapshots = _merge_book_into_rows(({**row, **previous} for row in rows), book)
    return {
        "status": "ok",
            "provider": f"{PROVIDER}+{previous.get('prev_day_provider') or 'previous_day_volume'}",
        "reason": None,
        "snapshots": snapshots,
    }


def _fetch_one_real_auction_snapshot(
    code: str,
    auction_rows: Mapping[str, list[dict[str, Any]]],
    *,
    asof: date | str | None,
    previous_day_metrics: Mapping[str, Mapping[str, Any]] | None,
    require_previous_day_metrics: bool,
    client: Any,
    budget_exhausted: Callable[[], bool],
    deadline_seconds: float | None,
) -> tuple[str, list[dict[str, Any]], str | None]:
    bare_code = _bare_code(code)
    if not require_previous_day_metrics:
        return bare_code, auction_rows[bare_code], None
    previous = dict((previous_day_metrics or {}).get(bare_code) or {})
    if not previous or not previous.get("prev_close"):
        if budget_exhausted():
            return bare_code, [], _budget_reason(deadline_seconds)
        enriched = fetch_previous_day_metrics(code, asof=asof, easy_tdx_client=client)
        if enriched:
            previous = {**previous, **enriched}
    prev_day_volume = _number(previous.get("prev_day_volume"))
    if prev_day_volume is None or prev_day_volume <= 0:
        return bare_code, [], "prev_day_volume 缺失或无效（mootdx 与腾讯历史 K 线均失败）"
    return bare_code, [{**row, **previous} for row in auction_rows[bare_code]], None


def fetch_real_auction_snapshots(
    codes: Iterable[str],
    *,
    asof: date | str | None = None,
    previous_day_metrics: Mapping[str, Mapping[str, Any]] | None = None,
    require_previous_day_metrics: bool = True,
    deadline_seconds: float | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Batch real observations with one easy_tdx connection and failure reasons.

    ``deadline_seconds`` bounds the wall-clock spent going out to the network.
    Without it the only bound is the cron job's own ``timeout_seconds``: hitting
    that is a SIGKILL, which loses both the rows already collected and any record
    of what the remaining symbols were waiting on. The budget only gates *new*
    fetches — rows already in hand are always returned.
    """
    unique = list(dict.fromkeys(str(code) for code in codes if code))
    snapshots: dict[str, list[dict[str, Any]]] = {}
    failures: dict[str, str] = {}
    deadline = (
        None if deadline_seconds is None else monotonic() + max(0.0, float(deadline_seconds))
    )

    def _budget_exhausted() -> bool:
        return deadline is not None and monotonic() >= deadline

    try:
        from easy_tdx import MacClient

        with MacClient.from_best_host() as client:
            # Historical volume is an independent HTTP fallback per symbol.
            # Keep the single easy_tdx connection above, but do not serialize
            # hundreds of Tencent K-line requests behind one failed mootdx
            # probe; the caller passes its remaining budget as deadline_seconds.
            auction_rows: dict[str, list[dict[str, Any]]] = {}
            for index, code in enumerate(unique):
                if _budget_exhausted():
                    for remaining in unique[index:]:
                        failures.setdefault(
                            _bare_code(remaining), _budget_reason(deadline_seconds)
                        )
                    break
                rows = _fetch_easy_tdx_with_client(client, code)
                if not rows:
                    failures[_bare_code(code)] = "easy_tdx 0x123D 无有效 09:15-09:25 竞价数据"
                else:
                    auction_rows[_bare_code(code)] = rows

            valid_codes = [code for code in unique if _bare_code(code) in auction_rows]
            books = _fetch_tencent_books(
                valid_codes,
                budget_exhausted=_budget_exhausted,
                deadline_seconds=deadline_seconds,
            )
            with ThreadPoolExecutor(max_workers=PREVIOUS_DAY_WORKERS) as pool:
                def fetch_one(code: str) -> tuple[str, list[dict[str, Any]], str | None]:
                    return _fetch_one_real_auction_snapshot(
                        code,
                        auction_rows,
                        asof=asof,
                        previous_day_metrics=previous_day_metrics,
                        require_previous_day_metrics=require_previous_day_metrics,
                        client=client,
                        budget_exhausted=_budget_exhausted,
                        deadline_seconds=deadline_seconds,
                    )
                for code, rows, failure in pool.map(fetch_one, valid_codes):
                    if failure:
                        failures[code] = failure
                    else:
                        snapshots[code] = _merge_book_into_rows(
                            rows, books.get(code, _book_fields(None))
                        )
    except ImportError:
        reason = "easy_tdx 未安装"
        failures = {_bare_code(code): reason for code in unique}
    except Exception as exc:
        reason = f"easy_tdx MacClient/通达信服务器不可用: {type(exc).__name__}: {exc}"
        failures = {_bare_code(code): reason for code in unique}
    return snapshots, failures
