"""Canonical market-data adapters for the live D0/D1 selection pipeline.

Eastmoney push2/push2his is treated as a degraded last-resort source for
quote, kline, board, and fund-flow paths.  The resilient adapters prefer
AkShare routes backed by Sina/Tencent/THS, then adata, then Eastmoney
datacenter-web where that dataset exists, and only probe push2 after those
routes fail or are unavailable.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from a_stock_http import (
    fetch_tencent_kline as _fetch_tencent_kline,
    fetch_tencent_minute as _fetch_tencent_minute,
    fetch_tencent_quote as _fetch_tencent_quote,
    fetch_tencent_snapshot as _fetch_tencent_snapshot,
    fetch_tencent_quotes_result as _fetch_tencent_quotes_result,
)
from http_client import DataSourceError, request_bytes
from paths import cache_dir
from provider_contract import transport_contract
from state_store import atomic_write_json, read_json


ADAPTER_VERSIONS = {
    "resilient_market_data": "multi-source-market-adapter-v1",
    "akshare_sina": "akshare-sina-adapter-v1",
    "akshare_tencent": "akshare-tencent-adapter-v1",
    "akshare_ths": "akshare-ths-adapter-v1",
    "adata": "adata-adapter-v1",
    "eastmoney_datacenter": "eastmoney-datacenter-adapter-v1",
    "eastmoney_push2_degraded": "eastmoney-push2-degraded-adapter-v1",
    "tencent_quote": "tencent-adapter-v2",
    "tencent_kline": "tencent-kline-adapter-v2",
    "tencent_minute": "tencent-adapter-v1",
    "tencent_orderbook": "tencent-adapter-v2",
    "akshare_limitup": "akshare-adapter-v1",
    "akshare_spot": "akshare-adapter-v2",
    "ths_industry_catalog": "akshare-ths-adapter-v1",
    "eastmoney_kline": "eastmoney-kline-adapter-v2",
}
SERIES_PROVENANCE_SCHEMA = "market_series_provenance_v1"
KNOWN_ADJUSTMENTS = {"qfq", "hfq", "unadjusted", "none"}
KNOWN_DECISION_MODES = {"live", "replay"}

_AKSHARE_PACE_SECONDS = 0.35
_CACHE_SCHEMA = "market_adapter_cache_v1"
_EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (A-Stock-Agent; degraded push2 probe)",
    "Referer": "https://quote.eastmoney.com/",
}


def _endpoint_key(endpoint: str) -> str:
    return endpoint.replace("/", "_").strip("_") or "default"


def _cache_file(kind: str, key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return os.path.join(cache_dir("stock-triage"), "market_adapters", kind, f"{digest}.json")


def _cache_get(kind: str, key: str, max_age_seconds: float) -> Any | None:
    payload = read_json(_cache_file(kind, key), {})
    if not isinstance(payload, dict) or payload.get("schema") != _CACHE_SCHEMA:
        return None
    try:
        age = time.time() - float(payload.get("stored_epoch") or 0)
    except (TypeError, ValueError):
        return None
    if age < 0 or age > max_age_seconds:
        return None
    return payload.get("data")


def _cache_set(kind: str, key: str, data: Any) -> None:
    try:
        atomic_write_json(_cache_file(kind, key), {
            "schema": _CACHE_SCHEMA,
            "stored_at": datetime.now().isoformat(timespec="seconds"),
            "stored_epoch": time.time(),
            "data": data,
        })
    except Exception:  # noqa: BLE001 - cache writes must not affect data fetches
        pass


def _recorded_call(provider: str, endpoint: str, func: Callable[[], Any]) -> Any:
    """Run a library-backed provider under the shared circuit breaker."""
    try:
        import provider_health
    except Exception:  # noqa: BLE001
        provider_health = None  # type: ignore[assignment]

    endpoint_class = _endpoint_key(endpoint)
    probe_token = None
    if provider_health is not None:
        decision = provider_health.allow_request(provider, endpoint_class)
        if not decision.get("allowed"):
            raise DataSourceError(provider, f"circuit {decision.get('state')} for {endpoint}")
        probe_token = decision.get("probe_token")
    started = time.monotonic()
    try:
        value = func()
    except Exception as exc:  # noqa: BLE001
        if provider_health is not None:
            provider_health.record_result(
                provider,
                endpoint_class,
                False,
                (time.monotonic() - started) * 1000,
                probe_token=probe_token,
            )
        if isinstance(exc, DataSourceError):
            raise
        raise DataSourceError(provider, f"{endpoint} failed: {exc}", exc) from exc
    if provider_health is not None:
        provider_health.record_result(
            provider,
            endpoint_class,
            True,
            (time.monotonic() - started) * 1000,
            probe_token=probe_token,
        )
    return value


def _fallback_chain(
    dataset: str,
    attempts: Sequence[tuple[str, Callable[[], Any]]],
    *,
    empty: Any,
    return_provider: bool = False,
) -> Any:
    errors: list[str] = []
    for provider, fetcher in attempts:
        try:
            value = _recorded_call(provider, dataset, fetcher)
        except DataSourceError as exc:
            errors.append(f"{provider}: {exc.message}")
            continue
        if _is_non_empty(value):
            return (value, provider) if return_provider else value
        errors.append(f"{provider}: empty")
    if errors:
        raise DataSourceError("market_adapters", f"{dataset} all providers failed: {'; '.join(errors)}")
    return (empty, None) if return_provider else empty


def _is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    try:
        import pandas as pd

        if isinstance(value, pd.DataFrame):
            return not value.empty
    except Exception:  # noqa: BLE001
        pass
    if isinstance(value, (list, tuple, dict, set, str, bytes)):
        return bool(value)
    return True


def fetch_with_replay_contract(
    fetcher: Callable[[], Any],
    *,
    provider: str,
    event_asof: str,
    supports_historical_replay: bool,
    current_date: str | None = None,
) -> dict[str, Any]:
    """Run a provider only when its replay capability matches the requested date."""
    requested = date.fromisoformat(str(event_asof))
    today = date.fromisoformat(current_date) if current_date else date.today()
    if requested > today:
        return {"status": "future_event_asof", "provider": provider, "data": None}
    if requested < today and not supports_historical_replay:
        return {
            "status": "historical_replay_unsupported",
            "provider": provider,
            "data": None,
        }
    return {"status": "ok", "provider": provider, "data": fetcher()}


def validate_series_provenance(provenance: Mapping[str, Any]) -> dict[str, Any]:
    """Return conservative directional eligibility for one normalized bar series."""
    if not isinstance(provenance, Mapping):
        return {"directional_eligible": False, "reason": "provenance_unknown"}
    if provenance.get("schema") != SERIES_PROVENANCE_SCHEMA:
        return {"directional_eligible": False, "reason": "provenance_unknown"}
    required = ("provider", "provider_version", "fetched_at", "event_asof")
    if any(not str(provenance.get(field) or "").strip() for field in required):
        return {"directional_eligible": False, "reason": "provenance_unknown"}
    adjustment = provenance.get("adjustment")
    if adjustment not in KNOWN_ADJUSTMENTS:
        return {"directional_eligible": False, "reason": "adjustment_unknown"}
    decision_mode = provenance.get("decision_mode")
    if decision_mode not in KNOWN_DECISION_MODES:
        return {"directional_eligible": False, "reason": "decision_mode_unknown"}
    try:
        date.fromisoformat(str(provenance["event_asof"]))
        fetched_at = datetime.fromisoformat(str(provenance["fetched_at"]))
    except (TypeError, ValueError):
        return {"directional_eligible": False, "reason": "provenance_time_invalid"}
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        return {"directional_eligible": False, "reason": "provenance_time_invalid"}
    if date.fromisoformat(str(provenance["event_asof"])) > date.today():
        return {"directional_eligible": False, "reason": "future_event_asof"}
    if fetched_at.astimezone(timezone.utc) > datetime.now(timezone.utc):
        return {"directional_eligible": False, "reason": "future_fetched_at"}
    return {"directional_eligible": True, "reason": "provenance_valid"}


def validate_series_content(data: Any, *, event_asof: str) -> dict[str, Any]:
    """Reject an entire bar series on future, unordered, duplicate or invalid bars."""
    if not isinstance(data, list) or not data:
        return {"directional_eligible": False, "reason": "series_invalid"}
    try:
        cutoff = date.fromisoformat(event_asof)
    except (TypeError, ValueError):
        return {"directional_eligible": False, "reason": "event_asof_invalid"}
    previous: date | None = None
    for bar in data:
        if not isinstance(bar, Mapping):
            return {"directional_eligible": False, "reason": "series_invalid"}
        try:
            bar_date = date.fromisoformat(str(bar["date"]))
            if any(
                isinstance(bar.get(field), bool)
                for field in ("open", "high", "low", "close", "volume")
            ):
                return {"directional_eligible": False, "reason": "series_invalid"}
            values = {
                field: float(bar[field])
                for field in ("open", "high", "low", "close", "volume")
            }
        except (KeyError, TypeError, ValueError):
            return {"directional_eligible": False, "reason": "series_invalid"}
        if bar_date > cutoff:
            return {"directional_eligible": False, "reason": "future_bar"}
        if previous is not None and bar_date <= previous:
            return {"directional_eligible": False, "reason": "series_order_invalid"}
        if (
            any(not math.isfinite(value) for value in values.values())
            or any(values[field] <= 0 for field in ("open", "high", "low", "close"))
            or values["volume"] < 0
            or values["low"] > min(values["open"], values["close"])
            or values["high"] < max(values["open"], values["close"])
            or values["low"] > values["high"]
        ):
            return {"directional_eligible": False, "reason": "series_invalid"}
        previous = bar_date
    return {"directional_eligible": True, "reason": "series_valid"}


def _normalize_series_contract(
    adjustment: Any, decision_mode: Any
) -> tuple[str, str] | None:
    normalized = (
        str(adjustment or "").strip().lower(),
        str(decision_mode or "").strip().lower(),
    )
    if normalized[0] not in KNOWN_ADJUSTMENTS or normalized[1] not in KNOWN_DECISION_MODES:
        return None
    return normalized


def _validate_series_request_times(event_asof: str, fetched_at: str) -> str | None:
    try:
        event_day = date.fromisoformat(str(event_asof))
        fetched = datetime.fromisoformat(str(fetched_at))
    except (TypeError, ValueError):
        return "series_time_invalid"
    if event_day > date.today():
        return "future_event_asof"
    if fetched.tzinfo is None or fetched.utcoffset() is None:
        return "series_time_invalid"
    if fetched.astimezone(timezone.utc) > datetime.now(timezone.utc):
        return "future_fetched_at"
    return None


def _blocked_series(reason: str, failures: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "status": "blocked",
        "reason": reason,
        "data": None,
        "series_provenance": None,
        "directional_eligible": False,
        "provider_attempts": failures or [],
    }


def _cached_series_is_eligible(
    cached: dict[str, Any],
    *,
    event_asof: str,
    adjustment: str,
    decision_mode: str,
) -> bool:
    provenance = cached.get("series_provenance") or {}
    if not isinstance(provenance, Mapping):
        return False
    return (
        cached.get("status") == "ok"
        and cached.get("directional_eligible") is True
        and validate_series_provenance(provenance)["directional_eligible"]
        and validate_series_content(cached.get("data"), event_asof=event_asof)[
            "directional_eligible"
        ]
        and provenance.get("event_asof") == event_asof
        and provenance.get("adjustment") == adjustment
        and provenance.get("decision_mode") == decision_mode
    )


def select_series_provider(
    attempts: Sequence[tuple[str, str, Callable[[], Any]]],
    *,
    adjustment: str,
    event_asof: str,
    fetched_at: str,
    decision_mode: str,
) -> dict[str, Any]:
    """Select the first non-empty series while preserving its actual source identity."""
    contract = _normalize_series_contract(adjustment, decision_mode)
    if contract is None:
        return _blocked_series("series_contract_invalid")
    normalized_adjustment, normalized_decision_mode = contract
    time_error = _validate_series_request_times(event_asof, fetched_at)
    if time_error:
        return _blocked_series(time_error)
    if normalized_decision_mode == "replay" and normalized_adjustment in {"qfq", "hfq"}:
        return _blocked_series("adjustment_replay_unsafe")
    failures: list[dict[str, str]] = []
    for provider, provider_version, fetcher in attempts:
        try:
            data = fetcher()
        except (DataSourceError, ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            failures.append({"provider": provider, "error_type": type(exc).__name__})
            continue
        if not _is_non_empty(data):
            failures.append({"provider": provider, "error_type": "empty"})
            continue
        content = validate_series_content(data, event_asof=event_asof)
        if not content["directional_eligible"]:
            return _blocked_series(content["reason"], failures)
        provenance = {
            "schema": SERIES_PROVENANCE_SCHEMA,
            "provider": provider,
            "provider_version": provider_version,
            "adjustment": normalized_adjustment,
            "event_asof": event_asof,
            "fetched_at": fetched_at,
            "decision_mode": normalized_decision_mode,
        }
        validation = validate_series_provenance(provenance)
        return {
            "status": "ok" if validation["directional_eligible"] else "blocked",
            "reason": validation["reason"],
            "data": data,
            "series_provenance": provenance,
            "directional_eligible": validation["directional_eligible"],
            "provider_attempts": failures,
        }
    return {
        "status": "data_unavailable",
        "reason": "provenance_unknown",
        "data": None,
        "series_provenance": None,
        "directional_eligible": False,
        "provider_attempts": failures,
    }


def _frame_records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    try:
        import pandas as pd

        if isinstance(frame, pd.DataFrame):
            return frame.where(pd.notna(frame), None).to_dict("records")
    except Exception:  # noqa: BLE001
        pass
    if isinstance(frame, list):
        return [dict(item) for item in frame if isinstance(item, dict)]
    return []


def _records_frame(records: list[dict[str, Any]]):
    import pandas as pd

    return pd.DataFrame(records)


def _number(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _money_to_yi(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    text = str(value).replace(",", "").strip()
    multiplier = 1.0
    if text.endswith("亿"):
        multiplier = 1.0
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = 0.0001
        text = text[:-1]
    number = _number(text)
    return None if number is None else number * multiplier


def _normal_code(code: Any) -> str:
    text = str(code or "").strip().lower()
    if text.startswith(("sh", "sz", "bj")):
        text = text[2:]
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6) if text else ""


def _market(code: str) -> str:
    normalized = _normal_code(code)
    return "sh" if normalized.startswith("6") else "sz"


def _secid(code: str, market: str | None = None) -> str:
    normalized = _normal_code(code)
    market = (market or _market(normalized)).lower()
    return f"1.{normalized}" if market == "sh" else f"0.{normalized}"


def _normalize_spot_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in records:
        code = _normal_code(row.get("代码") or row.get("code") or row.get("stock_code"))
        if not code:
            continue
        normalized = dict(row)
        normalized.setdefault("代码", code)
        normalized.setdefault("名称", row.get("名称") or row.get("name") or row.get("short_name") or code)
        normalized.setdefault("最新价", row.get("最新价") or row.get("trade") or row.get("close") or row.get("price"))
        normalized.setdefault("涨跌幅", row.get("涨跌幅") or row.get("changepercent") or row.get("change_pct"))
        normalized.setdefault("成交额", row.get("成交额") or row.get("amount") or row.get("turnover"))
        normalized.setdefault("成交量", row.get("成交量") or row.get("volume"))
        normalized.setdefault("今开", row.get("今开") or row.get("open"))
        normalized.setdefault("昨收", row.get("昨收") or row.get("settlement") or row.get("prev_close"))
        normalized.setdefault("最高", row.get("最高") or row.get("high"))
        normalized.setdefault("最低", row.get("最低") or row.get("low"))
        output.append(normalized)
    return output


def _normalize_bar_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    for row in records:
        raw_date = row.get("date") or row.get("日期") or row.get("trade_date") or row.get("trade_time")
        if raw_date in (None, ""):
            continue
        opened = _number(row.get("open") or row.get("开盘") or row.get("open_price"))
        close = _number(row.get("close") or row.get("收盘") or row.get("close_price"))
        high = _number(row.get("high") or row.get("最高") or row.get("high_price"))
        low = _number(row.get("low") or row.get("最低") or row.get("low_price"))
        if opened is None or close is None or high is None or low is None:
            continue
        volume = _number(row.get("volume") or row.get("成交量") or row.get("vol"))
        amount = _number(row.get("amount") or row.get("成交额"))
        bars.append({
            "date": str(raw_date)[:10],
            "open": opened,
            "close": close,
            "high": high,
            "low": low,
            "volume": volume or 0,
            "amount": amount or 0,
        })
    return bars


def _normalize_flow_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in records:
        date_value = row.get("日期") or row.get("date") or row.get("trade_date")
        if not date_value:
            continue
        main = (
            row.get("主力净流入-净额")
            or row.get("主力净流入")
            or row.get("main_net_inflow")
            or row.get("主力净额")
        )
        retail = row.get("小单净流入-净额") or row.get("retail_net_inflow") or row.get("小单净额")
        output.append({
            "date": str(date_value)[:10],
            "main_net_yi": (_number(main) or 0) / 100000000,
            "retail_net_yi": (_number(retail) or 0) / 100000000,
            "raw": row,
        })
    return output


def fetch_a_share_spot():
    """Fetch full-market A-share spot data without relying on push2 clist/get."""
    cache_key = "all"
    cached = _cache_get("spot", cache_key, max_age_seconds=300)
    if isinstance(cached, list) and cached:
        fetch_a_share_spot.last_source = "cache"
        return _records_frame(cached)

    def akshare_sina() -> list[dict[str, Any]]:
        import akshare as ak

        # stock_zh_a_spot is the Sina route; stock_zh_a_spot_em paginates push2.
        records = _frame_records(ak.stock_zh_a_spot())
        time.sleep(_AKSHARE_PACE_SECONDS)
        return _normalize_spot_records(records)

    def adata_spot() -> list[dict[str, Any]]:
        import adata

        return _normalize_spot_records(_frame_records(adata.stock.market.list_market_current()))

    def datacenter_spot() -> list[dict[str, Any]]:
        from eastmoney_intelligence import datacenter_query

        rows = datacenter_query(
            "RPTA_WEB_QUOTE",
            columns="SECURITY_CODE,SECURITY_NAME_ABBR,LATEST_PRICE,CHANGE_RATE,TURNOVER",
            page_size=6000,
        )
        return _normalize_spot_records([
            {
                "代码": row.get("SECURITY_CODE"),
                "名称": row.get("SECURITY_NAME_ABBR"),
                "最新价": row.get("LATEST_PRICE"),
                "涨跌幅": row.get("CHANGE_RATE"),
                "成交额": row.get("TURNOVER"),
            }
            for row in rows
        ])

    def eastmoney_push2_spot() -> list[dict[str, Any]]:
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get?"
            "pn=1&pz=6000&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
            "&fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
            "&fields=f12,f14,f2,f3,f5,f6,f15,f16,f17,f18"
        )
        result = request_bytes(
            url,
            source="eastmoney_push2_degraded",
            timeout=8,
            max_attempts=1,
            headers=_EASTMONEY_HEADERS,
        )
        payload = json.loads(result.data)
        diff = ((payload.get("data") or {}).get("diff") or [])
        return _normalize_spot_records([
            {
                "代码": row.get("f12"),
                "名称": row.get("f14"),
                "最新价": row.get("f2"),
                "涨跌幅": row.get("f3"),
                "成交量": row.get("f5"),
                "成交额": row.get("f6"),
                "最高": row.get("f15"),
                "最低": row.get("f16"),
                "今开": row.get("f17"),
                "昨收": row.get("f18"),
            }
            for row in diff
        ])

    records, provider = _fallback_chain(
        "a_share_spot",
        (
            ("akshare_sina", akshare_sina),
            ("adata", adata_spot),
            ("eastmoney_datacenter", datacenter_spot),
            ("mootdx", lambda: []),  # mootdx spot is catalog-only; live quotes use bars/quote
            ("eastmoney_push2_degraded", eastmoney_push2_spot),
        ),
        empty=[],
        return_provider=True,
    )
    fetch_a_share_spot.last_source = provider or "unknown"
    _cache_set("spot", cache_key, records)
    return _records_frame(records)


def fetch_a_share_quote(code: str) -> dict[str, Any]:
    """Single-stock quote, sourced from the cached full-market route first."""
    normalized = _normal_code(code)
    try:
        spot = fetch_a_share_spot()
        rows = spot[spot["代码"].astype(str).str.zfill(6) == normalized]
        if not rows.empty:
            row = rows.iloc[0].to_dict()
            return {
                "code": normalized,
                "name": row.get("名称"),
                "price": _number(row.get("最新价")),
                "change_pct": _number(row.get("涨跌幅")),
                "amount": _number(row.get("成交额")),
                "volume": _number(row.get("成交量")),
                "open": _number(row.get("今开")),
                "prev_close": _number(row.get("昨收")),
                "high": _number(row.get("最高")),
                "low": _number(row.get("最低")),
                "provider": "resilient_spot",
            }
    except Exception:
        pass
    market = _market(normalized)
    quote = fetch_tencent_quote([f"{market}{normalized}"]).get(f"{market}{normalized}") or {}
    return {**quote, "code": normalized, "provider": "tencent_fallback"}


def _mootdx_kline(code: str, *, days: int = 60) -> list[dict[str, Any]]:
    """Thin wrapper so the fallback-chain lambda can call mootdx lazily."""
    try:
        from mootdx_adapter import fetch_mootdx_bars
        bars = fetch_mootdx_bars(code, days=days)
        return _normalize_bar_records(bars)[-days:] if bars else []
    except Exception:
        return []


def fetch_a_share_daily_kline(
    code: str,
    *,
    market: str | None = None,
    days: int = 70,
    event_asof: str | None = None,
    adjustment: str = "qfq",
    decision_mode: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch daily OHLCV bars through AkShare/adata/cache before push2."""
    if event_asof is not None:
        series = fetch_a_share_daily_series(
            code,
            market=market,
            days=days,
            event_asof=event_asof,
            adjustment=adjustment,
            decision_mode=decision_mode or "replay",
        )
        return list(series.get("data") or []) if series.get("status") == "ok" else []
    normalized = _normal_code(code)
    market = (market or _market(normalized)).lower()
    cache_key = f"{market}{normalized}:{days}"
    cached = _cache_get("daily_kline", cache_key, max_age_seconds=12 * 3600)
    if isinstance(cached, list) and cached:
        return cached[-days:]

    end = date.today()
    start = end - timedelta(days=max(days * 2, days + 20))

    def akshare_tencent() -> list[dict[str, Any]]:
        import akshare as ak

        symbol = f"{market}{normalized}"
        df = ak.stock_zh_a_hist_tx(
            symbol=symbol,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        time.sleep(_AKSHARE_PACE_SECONDS)
        return _normalize_bar_records(_frame_records(df))[-days:]

    def adata_daily() -> list[dict[str, Any]]:
        import adata

        df = adata.stock.market.get_market(
            normalized,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            k_type=1,
            adjust_type=1,
        )
        return _normalize_bar_records(_frame_records(df))[-days:]

    def tencent_direct() -> list[dict[str, Any]]:
        return _fetch_tencent_kline(normalized, market=market, days=days, ktype="day")

    def eastmoney_push2_kline() -> list[dict[str, Any]]:
        return _fetch_eastmoney_push2_kline(normalized, market=market, days=days)

    try:
        bars = _fallback_chain(
            "daily_kline",
            (
                ("akshare", akshare_tencent),
                ("adata", adata_daily),
                ("mootdx", lambda: _mootdx_kline(normalized, days=days)),
                ("tencent", tencent_direct),
                ("eastmoney_push2_degraded", eastmoney_push2_kline),
            ),
            empty=[],
        )
    except DataSourceError:
        return []
    _cache_set("daily_kline", cache_key, bars)
    return bars[-days:]


def _daily_series_attempts(
    code: str,
    market: str,
    days: int,
    event_asof: str,
    adjustment: str,
    decision_mode: str,
) -> tuple[tuple[str, str, Callable[[], Any]], ...]:
    end = date.fromisoformat(event_asof)
    start = end - timedelta(days=max(days * 2, days + 20))

    def akshare_tencent() -> list[dict[str, Any]]:
        if adjustment != "qfq":
            return []
        import akshare as ak

        frame = ak.stock_zh_a_hist_tx(
            symbol=f"{market}{code}",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        time.sleep(_AKSHARE_PACE_SECONDS)
        return _normalize_bar_records(_frame_records(frame))[-days:]

    def adata_daily() -> list[dict[str, Any]]:
        import adata

        adjust_type = {"none": 0, "unadjusted": 0, "qfq": 1, "hfq": 2}.get(adjustment)
        if adjust_type is None:
            return []
        frame = adata.stock.market.get_market(
            code,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            k_type=1,
            adjust_type=adjust_type,
        )
        return _normalize_bar_records(_frame_records(frame))[-days:]

    def mootdx_historical() -> list[dict[str, Any]] | None:
        from mootdx_adapter import fetch_mootdx_bars
        bars = fetch_mootdx_bars(code, days=days)
        return bars or None

    def tencent_current_only() -> Any:
        result = fetch_with_replay_contract(
            lambda: _fetch_tencent_kline(code, market=market, days=days, ktype="day"),
            provider="tencent",
            event_asof=event_asof,
            supports_historical_replay=False,
        )
        return result.get("data") if result["status"] == "ok" else None

    def eastmoney_date_bound() -> list[dict[str, Any]]:
        return _fetch_eastmoney_push2_kline(
            code,
            market=market,
            days=days,
            event_asof=event_asof,
            adjustment=adjustment,
        )

    return (
        ("akshare_tencent", ADAPTER_VERSIONS["akshare_tencent"], akshare_tencent),
        ("adata", ADAPTER_VERSIONS["adata"], adata_daily),
        ("mootdx", ADAPTER_VERSIONS.get("mootdx", "mootdx-adapter-v1"), mootdx_historical),
        ("tencent_kline", ADAPTER_VERSIONS["tencent_kline"], tencent_current_only),
        (
            "eastmoney_push2_degraded",
            ADAPTER_VERSIONS["eastmoney_push2_degraded"],
            eastmoney_date_bound,
        ),
    )


def fetch_a_share_daily_series(
    code: str,
    *,
    market: str | None = None,
    days: int = 70,
    event_asof: str,
    adjustment: str = "qfq",
    decision_mode: str = "replay",
    fetched_at: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Fetch a PIT-bound daily series with complete series-level provenance."""
    contract = _normalize_series_contract(adjustment, decision_mode)
    if contract is None:
        return _blocked_series("series_contract_invalid")
    normalized_adjustment, normalized_decision_mode = contract
    if normalized_adjustment in {"qfq", "hfq"} and (
        normalized_decision_mode == "replay" or event_asof != date.today().isoformat()
    ):
        return _blocked_series("adjustment_replay_unsafe")
    normalized = _normal_code(code)
    resolved_market = (market or _market(normalized)).lower()
    cache_key = (
        f"{resolved_market}{normalized}:{days}:{normalized_adjustment}:"
        f"{event_asof}:{normalized_decision_mode}"
    )
    if use_cache:
        cached = _cache_get("daily_series_v1", cache_key, max_age_seconds=12 * 3600)
        if isinstance(cached, dict) and _cached_series_is_eligible(
            cached,
            event_asof=event_asof,
            adjustment=normalized_adjustment,
            decision_mode=normalized_decision_mode,
        ):
            return cached
    timestamp = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    attempts = _daily_series_attempts(
        normalized,
        resolved_market,
        days,
        event_asof,
        normalized_adjustment,
        normalized_decision_mode,
    )
    result = select_series_provider(
        attempts,
        adjustment=normalized_adjustment,
        event_asof=event_asof,
        fetched_at=timestamp,
        decision_mode=normalized_decision_mode,
    )
    if use_cache and result["status"] == "ok":
        _cache_set("daily_series_v1", cache_key, result)
    return result


def _fetch_eastmoney_push2_kline(
    code: str,
    *,
    market: str,
    days: int = 70,
    event_asof: str | None = None,
    adjustment: str = "qfq",
) -> list[dict[str, Any]]:
    """Last-resort daily OHLCV bars from the degraded EastMoney push2his path."""
    secid = f"1.{code}" if market.lower() == "sh" else f"0.{code}"
    end_day = date.fromisoformat(event_asof) if event_asof else date.today()
    end = end_day.strftime("%Y%m%d")
    start = (end_day - timedelta(days=max(days * 2, days + 20))).strftime("%Y%m%d")
    fqt = {"none": 0, "unadjusted": 0, "qfq": 1, "hfq": 2}.get(adjustment)
    if fqt is None:
        return []
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        f"&klt=101&fqt={fqt}&beg={start}&end={end}"
    )
    result = request_bytes(
        url,
        source="eastmoney_kline",
        timeout=10,
        max_attempts=1,
        headers=_EASTMONEY_HEADERS,
    )
    try:
        data = json.loads(result.data)
    except Exception:
        return []
    klines = ((data.get("data") or {}).get("klines") or [])
    bars: list[dict[str, Any]] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        bars.append({
            "date": parts[0],
            "open": float(parts[1]),
            "close": float(parts[2]),
            "high": float(parts[3]),
            "low": float(parts[4]),
            "volume": int(float(parts[5])),
            "amount": float(parts[6]),
        })
    return bars[-days:]


def fetch_eastmoney_kline(
    code: str,
    *,
    market: str,
    days: int = 70,
) -> list[dict[str, Any]]:
    """Backward-compatible kline entry now routed through the resilient chain."""
    return fetch_a_share_daily_kline(code, market=market, days=days)


def fetch_stock_fund_flow(code: str, *, market: str | None = None, days: int = 3) -> dict[str, Any]:
    """Fetch individual stock fund-flow; returns {} when all exact sources fail."""
    normalized = _normal_code(code)
    market = (market or _market(normalized)).lower()
    cache_key = f"{market}{normalized}:{days}"
    cached = _cache_get("stock_fund_flow", cache_key, max_age_seconds=900)
    if isinstance(cached, dict) and cached:
        return cached

    def akshare_flow() -> dict[str, Any]:
        import akshare as ak

        records = _normalize_flow_records(_frame_records(ak.stock_individual_fund_flow(stock=normalized, market=market)))
        if not records:
            return {}
        latest = records[-1]
        return {**latest, "provider": "akshare"}

    def akshare_ths_rank() -> dict[str, Any]:
        import akshare as ak

        rows = _frame_records(ak.stock_fund_flow_individual(symbol="即时"))
        for row in rows:
            if _normal_code(row.get("股票代码")) != normalized:
                continue
            net_yi = _money_to_yi(row.get("净额"))
            if net_yi is None:
                return {}
            return {
                "date": date.today().isoformat(),
                "main_net_yi": round(net_yi, 4),
                "retail_net_yi": 0.0,
                "provider": "akshare_ths",
                "raw": row,
            }
        return {}

    def adata_flow() -> dict[str, Any]:
        import adata

        records = _normalize_flow_records(_frame_records(adata.stock.market.get_capital_flow(normalized)))
        if not records:
            return {}
        latest = records[-1]
        return {**latest, "provider": "adata"}

    def eastmoney_push2_flow() -> dict[str, Any]:
        payload = _fetch_eastmoney_push2_flow(_secid(normalized, market), days=days)
        return {**_parse_push2_flow_payload(payload), "provider": "eastmoney_push2_degraded"}

    try:
        value = _fallback_chain(
            "stock_fund_flow",
            (
                ("akshare", akshare_flow),
                ("akshare", akshare_ths_rank),
                ("adata", adata_flow),
                ("eastmoney_push2_degraded", eastmoney_push2_flow),
            ),
            empty={},
        )
    except DataSourceError:
        return {}
    _cache_set("stock_fund_flow", cache_key, value)
    return value


def fetch_sector_fund_flow(bk_code: str, *, name: str | None = None, days: int = 3) -> dict[str, Any]:
    """Fetch sector-level fund flow with THS board summary as the first route."""
    cache_key = f"{bk_code}:{name or ''}:{days}"
    cached = _cache_get("sector_fund_flow", cache_key, max_age_seconds=900)
    if isinstance(cached, dict) and cached:
        return cached

    def akshare_ths_sector() -> dict[str, Any]:
        import akshare as ak

        frame = ak.stock_board_industry_summary_ths()
        records = _frame_records(frame)
        target = str(name or "").strip()
        for row in records:
            if target and str(row.get("板块") or "") != target:
                continue
            net = _number(row.get("净流入"))
            if net is None:
                continue
            # THS summary reports 亿元 in the current AkShare payload.
            return {
                "date": date.today().isoformat(),
                "main_net_yi": net,
                "retail_net_yi": 0.0,
                "provider": "akshare_ths",
                "raw": row,
            }
        return {}

    def adata_sector() -> dict[str, Any]:
        import adata

        frame = adata.stock.market.get_market_concept_current_east(index_code=bk_code)
        rows = _frame_records(frame)
        if not rows:
            return {}
        return {"date": date.today().isoformat(), "provider": "adata", "raw": rows[-1]}

    def eastmoney_push2_sector() -> dict[str, Any]:
        payload = _fetch_eastmoney_push2_flow(f"90.{bk_code}", days=days)
        return {**_parse_push2_flow_payload(payload), "provider": "eastmoney_push2_degraded"}

    try:
        value = _fallback_chain(
            "sector_fund_flow",
            (
                ("akshare", akshare_ths_sector),
                ("adata", adata_sector),
                ("eastmoney_push2_degraded", eastmoney_push2_sector),
            ),
            empty={},
        )
    except DataSourceError:
        return {}
    _cache_set("sector_fund_flow", cache_key, value)
    return value


def _fetch_eastmoney_push2_flow(secid: str, *, days: int = 3) -> dict[str, Any]:
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?"
        f"fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56&lmt={days}&secid={secid}"
    )
    result = request_bytes(
        url,
        source="eastmoney_fund_flow_degraded",
        timeout=8,
        max_attempts=1,
        headers=_EASTMONEY_HEADERS,
    )
    return json.loads(result.data)


def _parse_push2_flow_payload(payload: dict[str, Any]) -> dict[str, Any]:
    klines = (payload.get("data") or {}).get("klines") or []
    if not klines:
        return {}
    parts = str(klines[-1]).split(",")
    if len(parts) < 6:
        return {}
    main = _number(parts[3])
    retail = _number(parts[5])
    if main is None or retail is None:
        return {}
    return {
        "date": parts[0],
        "main_net_yi": round(main / 10000, 1),
        "retail_net_yi": round(retail / 10000, 1),
    }


def _northbound_cache_dir() -> str:
    return os.path.join(cache_dir("stock-triage"), "northbound_cache")


def _northbound_csv_path() -> str:
    d = _northbound_cache_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "northbound_history.csv")


def _save_northbound_snapshot(date_str: str, net_flow_yi: float, provider: str) -> None:
    """Append a snapshot row to local CSV."""
    path = _northbound_csv_path()
    exists = os.path.isfile(path)
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not exists:
                writer.writerow(["date", "net_flow_yi", "provider", "recorded_at"])
            writer.writerow([date_str, net_flow_yi, provider, datetime.now().isoformat(timespec="seconds")])
    except Exception:  # noqa: BLE001 — cache writes must not break data flow
        pass


def _load_northbound_history(max_days: int = 30) -> list[dict[str, Any]]:
    """Load northbound CSV history, dedup by date (last write wins)."""
    path = _northbound_csv_path()
    if not os.path.isfile(path):
        return []
    records: dict[str, dict[str, Any]] = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                d = row.get("date", "")[:10]
                if d:
                    records[d] = {
                        "date": d,
                        "net_flow_yi": _number(row.get("net_flow_yi")),
                        "provider": row.get("provider", "csv_cache"),
                    }
    except Exception:  # noqa: BLE001
        pass
    result = sorted(records.values(), key=lambda r: r["date"], reverse=True)
    return result[:max_days]


def fetch_northbound_flow() -> dict[str, Any]:
    """Fetch northbound capital flow with CSV local cache for history.

    Every real-time snapshot is appended to a local CSV.  If the upstream API
    returns empty or NaN values, the CSV history is used as fallback so daily
    trend comparison never breaks.
    """
    cached = _cache_get("northbound", "summary", max_age_seconds=600)
    if isinstance(cached, dict) and cached:
        return cached

    def akshare_north() -> dict[str, Any]:
        import akshare as ak

        rows = _frame_records(ak.stock_hsgt_fund_flow_summary_em())
        for row in reversed(rows):
            net = _number(row.get("资金净流入") or row.get("成交净买额"))
            if net is None:
                continue
            if abs(net) > 100000:
                net /= 100000000
            result = {"date": str(row.get("交易日") or date.today())[:10], "net_flow_yi": round(net, 1), "provider": "akshare"}
            _save_northbound_snapshot(result["date"], result["net_flow_yi"], result["provider"])
            return result
        return {}

    def eastmoney_kamt() -> dict[str, Any]:
        url = (
            "https://push2his.eastmoney.com/api/qt/kamt.kline/get?"
            "fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54&klt=1&lmt=5&secid=1.000001"
        )
        result = request_bytes(
            url,
            source="eastmoney_kamt",
            timeout=8,
            max_attempts=1,
            headers=_EASTMONEY_HEADERS,
        )
        payload = json.loads(result.data)
        klines = (payload.get("data") or {}).get("klines") or []
        if not klines:
            return {}
        parts = str(klines[-1]).split(",")
        net = _number(parts[1] if len(parts) > 1 else None)
        if net is None:
            return {}
        result = {"date": parts[0], "net_flow_yi": round(net / 10000, 1), "provider": "eastmoney_kamt"}
        _save_northbound_snapshot(result["date"], result["net_flow_yi"], result["provider"])
        return result

    try:
        value = _fallback_chain(
            "northbound_flow",
            (("akshare", akshare_north), ("eastmoney_kamt", eastmoney_kamt)),
            empty={},
        )
    except DataSourceError:
        value = {}

    # If API returned empty/NaN, try CSV history as fallback
    if not value or value.get("net_flow_yi") is None:
        history = _load_northbound_history(max_days=5)
        if history:
            value = history[0]  # most recent from cache
            value["provider"] = value.get("provider", "csv_cache") + "_csv_fallback"

    _cache_set("northbound", "summary", value)
    return value


def fetch_board_quotes() -> list[dict[str, Any]]:
    """Fetch industry board quotes; THS avoids Eastmoney clist/get."""
    cached = _cache_get("board_quotes", "industry", max_age_seconds=300)
    if isinstance(cached, list) and cached:
        return cached

    def akshare_ths_boards() -> list[dict[str, Any]]:
        import akshare as ak

        rows = _frame_records(ak.stock_board_industry_summary_ths())
        return [
            {
                "f12": row.get("代码") or row.get("code") or row.get("板块"),
                "f14": row.get("板块"),
                "f3": _number(row.get("涨跌幅")),
                "f62": _number(row.get("净流入")),
                "raw": row,
                "provider": "akshare_ths",
            }
            for row in rows
            if row.get("板块")
        ]

    def adata_boards() -> list[dict[str, Any]]:
        import adata

        rows = _frame_records(adata.stock.info.all_concept_code_ths())
        return [
            {"f12": row.get("index_code") or row.get("code"), "f14": row.get("name"), "provider": "adata", "raw": row}
            for row in rows
            if row.get("name")
        ]

    def eastmoney_push2_boards() -> list[dict[str, Any]]:
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get?"
            "pn=1&pz=500&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
            "&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14,f3,f62,f104,f105"
        )
        result = request_bytes(
            url,
            source="eastmoney_board_quotes_degraded",
            timeout=8,
            max_attempts=1,
            headers=_EASTMONEY_HEADERS,
        )
        payload = json.loads(result.data)
        return list(((payload.get("data") or {}).get("diff") or []))

    try:
        boards = _fallback_chain(
            "board_quotes",
            (
                ("akshare", akshare_ths_boards),
                ("adata", adata_boards),
                ("eastmoney_push2_degraded", eastmoney_push2_boards),
            ),
            empty=[],
        )
    except DataSourceError:
        return []
    _cache_set("board_quotes", "industry", boards)
    return boards


def fetch_industry_boards() -> list[tuple[str, str]]:
    """Industry board code/name catalog used by industry_map refresh."""
    boards = fetch_board_quotes()
    return [
        (str(row.get("f12") or row.get("code") or row.get("raw", {}).get("code") or row.get("f14")), str(row.get("f14")))
        for row in boards
        if row.get("f14")
    ]


def fetch_dragon_tiger_rows(code: str, *, asof: date | str | None = None) -> dict[str, Any]:
    from eastmoney_intelligence import fetch_dragon_tiger

    return fetch_dragon_tiger(code, asof=asof)


def fetch_tencent_quote(codes: Sequence[str]) -> dict[str, dict[str, Any]]:
    return _fetch_tencent_quote(list(codes))


def fetch_tencent_quote_with_provenance(
    codes: Sequence[str],
    *,
    corroborating_quotes: Mapping[str, Mapping[str, Any]] | None = None,
    decision_stage: str | None = None,
    maximum_corroboration_age_seconds: int = 120,
) -> dict[str, dict[str, Any]]:
    """Tencent quotes remain non-directional until corroboration is adapter-bound."""
    result = _fetch_tencent_quotes_result(list(codes))
    trust = transport_contract("http://qt.gtimg.cn/")
    quotes = {
        code: {
            **quote,
            "provider_version": quote.get("provider_version")
            or ADAPTER_VERSIONS["tencent_quote"],
            "transport_trust": quote.get("transport_trust") or trust["trust"],
            "directional_eligible": False,
            "transport_reason": quote.get("transport_reason") or trust["reason"],
        }
        for code, quote in result.data.items()
    }
    if corroborating_quotes:
        for quote in quotes.values():
            quote["corroboration_status"] = "rejected_untrusted_input"
    return quotes


def fetch_tencent_snapshot(codes: Sequence[str]) -> dict[str, dict[str, Any]]:
    return _fetch_tencent_snapshot(list(codes))


def fetch_tencent_kline(
    code: str,
    *,
    market: str,
    days: int,
    ktype: str = "day",
) -> list[dict[str, Any]]:
    return _fetch_tencent_kline(code, market=market, days=days, ktype=ktype)


def fetch_tencent_minute(code: str, *, market: str) -> list[dict[str, Any]]:
    return _fetch_tencent_minute(code, market=market)


def fetch_hot_money_limitup_pool(date: str):
    try:
        import akshare as ak

        return ak.stock_zt_pool_em(date=date)
    except Exception as exc:  # noqa: BLE001
        raise DataSourceError("akshare_limitup", f"涨停池获取失败: {date}", exc) from exc


def fetch_hot_money_strong_pool(date: str):
    try:
        import akshare as ak

        return ak.stock_zt_pool_strong_em(date=date)
    except Exception as exc:  # noqa: BLE001
        raise DataSourceError("akshare_limitup", f"强势股池获取失败: {date}", exc) from exc


def fetch_industry_catalog_ths():
    try:
        import akshare as ak

        return ak.stock_board_industry_name_ths()
    except Exception as exc:  # noqa: BLE001
        raise DataSourceError(
            "ths_industry_catalog",
            "同花顺行业目录获取失败",
            exc,
        ) from exc


def fetch_industry_comparison() -> list[dict[str, Any]]:
    """获取行业对比排名（涨跌幅/成交额/净流入 TOP 行业）。

    用同花顺行业数据返回按涨跌幅排序的行业排名，
    包含涨跌幅、成交额、主力净流入等关键指标。
    用于板块轮动分析和行业筛选。

    Returns:
        list[dict]: 每行业 {name, code, change_pct, amount, net_inflow, rank}
        [] on failure.
    """
    def akshare_ths_comparison() -> list[dict[str, Any]]:
        import akshare as ak
        rows = _frame_records(ak.stock_board_industry_summary_ths())
        result = []
        for i, row in enumerate(rows, 1):
            change_pct = _number(row.get("涨跌幅"))
            if change_pct is None:
                continue
            result.append({
                "rank": i,
                "name": str(row.get("板块") or ""),
                "code": str(row.get("代码") or ""),
                "change_pct": round(change_pct, 2),
                "price": _number(row.get("最新价")),
                "amount_yi": round(_number(row.get("成交额", 0)) / 1e8, 2) if _number(row.get("成交额")) else None,
                "net_inflow_yi": round(_number(row.get("净流入", 0)) / 1e8, 2) if _number(row.get("净流入")) else None,
                "amount_change_pct": _number(row.get("量比")),
                "rising_stocks": int(_number(row.get("上涨股数", 0))),
                "falling_stocks": int(_number(row.get("下跌股数", 0))),
                "provider": "akshare_ths",
            })
        return result

    def eastmoney_industry_comparison() -> list[dict[str, Any]]:
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get?"
            "pn=1&pz=90&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
            "&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14,f2,f3,f4,f62,f184,f66,f69"
        )
        result = request_bytes(
            url,
            source="eastmoney_industry_comparison",
            timeout=10,
            max_attempts=1,
            headers=_EASTMONEY_HEADERS,
        )
        payload = json.loads(result.data)
        rows = ((payload.get("data") or {}).get("diff") or [])
        result = []
        for i, row in enumerate(rows, 1):
            change_pct = _number(row.get("f3"))
            if change_pct is None:
                continue
            result.append({
                "rank": i,
                "name": str(row.get("f14") or ""),
                "code": str(row.get("f12") or ""),
                "change_pct": round(change_pct, 2),
                "price": _number(row.get("f2")),
                "amount_yi": round(_number(row.get("f4", 0)) / 1e8, 2) if _number(row.get("f4")) else None,
                "net_inflow_yi": round(_number(row.get("f62", 0)) / 1e8, 2) if _number(row.get("f62")) else None,
                "provider": "eastmoney_push2_degraded",
            })
        return result

    try:
        return _fallback_chain(
            "industry_comparison",
            (
                ("akshare", akshare_ths_comparison),
                ("adata", eastmoney_industry_comparison),
            ),
            empty=[],
        )
    except DataSourceError:
        return []


def fetch_concept_boards() -> list[dict[str, Any]]:
    """获取东财概念板块行情（作为百度PAE禁用的替代）。

    返回概念板块涨跌幅/成交额/净流入排名，
    用于补充 baidu_attention 禁用的概念板块数据。

    Fallback 链：
    1. 优先：akshare stock_board_concept_name_em（走 akshare 的东财路由，非 push2）
    2. 降级：push2.eastmoney.com（m:90+t:3，当前WAF封锁中）
    """

    def akshare_concept() -> list[dict[str, Any]]:
        import akshare as ak

        df = ak.stock_board_concept_name_em()
        time.sleep(_AKSHARE_PACE_SECONDS)
        records = _frame_records(df)
        output = []
        for i, row in enumerate(records, 1):
            name = str(row.get("板块名称") or "")
            code = str(row.get("板块代码") or "")
            if not name:
                continue
            change_pct = _number(row.get("涨跌幅"))
            price = _number(row.get("最新价"))
            # stock_board_concept_name_em does not provide amount/成交额 or net_inflow,
            # so these are left as None (callers handle None gracefully)
            output.append({
                "rank": i,
                "name": name,
                "code": code,
                "change_pct": round(change_pct, 2) if change_pct is not None else 0.0,
                "price": price,
                "amount_yi": None,
                "net_inflow_yi": None,
                "provider": "akshare_concept_name_em",
            })
        return output

    def eastmoney_push2_concept() -> list[dict[str, Any]]:
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get?"
            "pn=1&pz=500&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
            "&fltt=2&invt=2&fid=f3&fs=m:90+t:3&fields=f12,f14,f2,f3,f4,f62,f184,f66,f69"
        )
        result = request_bytes(
            url,
            source="eastmoney_concept_board",
            timeout=10,
            max_attempts=1,
            headers=_EASTMONEY_HEADERS,
        )
        payload = json.loads(result.data)
        rows = ((payload.get("data") or {}).get("diff") or [])
        output = []
        for i, row in enumerate(rows, 1):
            name = str(row.get("f14") or "")
            if not name:
                continue
            output.append({
                "rank": i,
                "name": name,
                "code": str(row.get("f12") or ""),
                "change_pct": round(_number(row.get("f3")) or 0.0, 2),
                "price": _number(row.get("f2")),
                "amount_yi": round(_number(row.get("f4", 0)) / 1e8, 2) if _number(row.get("f4")) else None,
                "net_inflow_yi": round(_number(row.get("f62", 0)) / 1e8, 2) if _number(row.get("f62")) else None,
                "provider": "eastmoney_push2_degraded",
            })
        return output

    try:
        return _fallback_chain(
            "concept_boards",
            (
                ("akshare", akshare_concept),
                ("eastmoney_push2_degraded", eastmoney_push2_concept),
            ),
            empty=[],
        )
    except DataSourceError:
        return []


def fetch_tencent_index_overview():
    import pandas as pd

    response = request_bytes(
        "http://qt.gtimg.cn/q=sh000001,sz399001,sz399006",
        source="tencent",
        timeout=10,
        max_attempts=2,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    rows = []
    for line in response.data.decode("gbk", errors="ignore").strip().split("\n"):
        if "=" not in line:
            continue
        parts = line.split("=", 1)[1].strip('"').split("~")
        if len(parts) < 40:
            continue
        rows.append({
            "名称": parts[1],
            "代码": parts[2],
            "最新价": float(parts[3]) if parts[3] else 0,
            "涨跌幅": float(parts[32]) if parts[32] else 0,
            "涨跌额": float(parts[31]) if parts[31] else 0,
            "成交额": float(parts[37]) * 10000 if parts[37] else 0,
            "成交量": float(parts[36]) if parts[36] else 0,
            "最高": float(parts[33]) if parts[33] else 0,
            "最低": float(parts[34]) if parts[34] else 0,
            "今开": float(parts[5]) if parts[5] else 0,
            "昨收": float(parts[4]) if parts[4] else 0,
        })
    return pd.DataFrame(rows)
