"""Normalized Eastmoney adapters for institutional and chip intelligence.

Endpoint selection and field mappings are derived from simonlin1212/a-stock-data
3.2.2 (Apache-2.0), then adapted to this project's urllib transport, shared
configuration, typed failures, and cross-process rate limiting.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from data_access_config import provider_settings
from http_client import DataSourceError, ErrorType, build_request, request_json
from paths import cache_dir


DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_API_URL = "https://reportapi.eastmoney.com/report/list"
USER_AGENT = "Mozilla/5.0 (A-Stock-Agent; Eastmoney adapter)"
ADAPTER_VERSION = "eastmoney-intelligence-v2"
UPSTREAM_VERSION = "simonlin1212/a-stock-data@9379ab9"


def _settings() -> dict[str, Any]:
    return provider_settings("eastmoney")


def _coordination_dir() -> str:
    return os.path.join(
        cache_dir("stock-triage"),
        "provider_coordination",
        "eastmoney",
    )


def _rate_limit_file() -> str:
    return os.path.join(_coordination_dir(), "rate_limit.json")


# _health_file() removed in v3 — circuit breaker unified into provider_health.py


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=os.path.dirname(path),
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _lock_age_seconds(lock_dir: str) -> float:
    owner = _read_json(os.path.join(lock_dir, "owner.json"), {})
    try:
        created = float(owner.get("created_epoch"))
    except (AttributeError, TypeError, ValueError):
        try:
            created = os.stat(lock_dir).st_mtime
        except OSError:
            return 0.0
    return max(0.0, time.time() - created)


def _quarantine_lock(lock_dir: str, label: str) -> None:
    quarantine = f"{lock_dir}.{label}-{uuid.uuid4().hex}"
    try:
        os.replace(lock_dir, quarantine)
    except (FileNotFoundError, OSError):
        return
    shutil.rmtree(quarantine, ignore_errors=True)


@contextmanager
def _coordination_lock(
    name: str,
    *,
    timeout: float,
    stale_after: float,
):
    """Coordinate machines through atomic mkdir on the shared state volume."""
    settings = _settings()
    if settings.get("coordination_backend") != "shared_file":
        raise DataSourceError(
            "eastmoney",
            "unsupported coordination backend",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    root = _coordination_dir()
    os.makedirs(root, exist_ok=True)
    lock_dir = os.path.join(root, f"{name}.lock")
    token = uuid.uuid4().hex
    deadline = time.monotonic() + max(0.01, timeout)
    while True:
        try:
            os.mkdir(lock_dir)
            try:
                _write_json(
                    os.path.join(lock_dir, "owner.json"),
                    {
                        "token": token,
                        "pid": os.getpid(),
                        "created_epoch": time.time(),
                        "created_at": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                    },
                )
            except Exception:
                _quarantine_lock(lock_dir, "broken")
                raise
            break
        except FileExistsError:
            if _lock_age_seconds(lock_dir) > stale_after:
                _quarantine_lock(lock_dir, "stale")
                continue
            if time.monotonic() >= deadline:
                raise DataSourceError(
                    "eastmoney",
                    f"coordination lock timeout: {name}",
                    error_type=ErrorType.TIMEOUT,
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        owner = _read_json(os.path.join(lock_dir, "owner.json"), {})
        if owner.get("token") == token:
            _quarantine_lock(lock_dir, "released")


def _wait_for_provider_slot(
    *,
    minimum_interval: float | None = None,
    jitter_max: float | None = None,
) -> None:
    """Serialize calls across Hermes/OpenClaw machines sharing state."""
    settings = _settings()
    interval = (
        float(settings["minimum_interval_seconds"])
        if minimum_interval is None
        else minimum_interval
    )
    jitter = (
        float(settings["jitter_max_seconds"])
        if jitter_max is None
        else jitter_max
    )
    path = _rate_limit_file()
    with _coordination_lock(
        "rate_limit",
        timeout=float(settings["coordination_timeout_seconds"]),
        stale_after=float(settings["coordination_stale_seconds"]),
    ):
        state = _read_json(path, {})
        now = time.time()
        wait = interval - (now - float(state.get("last_call_epoch") or 0))
        if wait > 0:
            time.sleep(wait + random.uniform(0, max(0.0, jitter)))
        _write_json(
            path,
            {
                "schema": "provider_rate_limit_v2",
                "provider": "eastmoney",
                "coordination_backend": settings["coordination_backend"],
                "last_call_epoch": time.time(),
            },
        )


def _invalid_response(
    message: str,
    *,
    status_code: int | None = None,
) -> DataSourceError:
    return DataSourceError(
        "eastmoney",
        message,
        error_type=ErrorType.INVALID_RESPONSE,
        status_code=status_code,
    )


def _business_status(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _invalid_response("response root must be an object")
    if payload.get("success") is False:
        code = payload.get("code")
        status = int(code) if str(code).isdigit() else None
        raise _invalid_response(
            str(payload.get("message") or payload.get("msg") or "business failure"),
            status_code=status,
        )
    for key in ("code", "rc"):
        if key not in payload or payload.get(key) in (None, "", 0, "0"):
            continue
        raw_code = payload.get(key)
        status = int(raw_code) if str(raw_code).isdigit() else None
        raise _invalid_response(
            str(payload.get("message") or payload.get("msg") or f"{key}={raw_code}"),
            status_code=status,
        )
    return payload


def _validate_datacenter(payload: Any) -> list[dict[str, Any]]:
    if (
        isinstance(payload, dict)
        and str(payload.get("code") or "") == "9201"
        and str(payload.get("message") or payload.get("msg") or "") == "返回数据为空"
    ):
        return []
    checked = _business_status(payload)
    result = checked.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise _invalid_response("datacenter result.data must be a list")
    rows = result["data"]
    if any(not isinstance(row, dict) for row in rows):
        raise _invalid_response("datacenter result.data contains a non-object row")
    return [dict(row) for row in rows]


def _validate_reports(payload: Any) -> list[dict[str, Any]]:
    checked = _business_status(payload)
    rows = checked.get("data")
    if not isinstance(rows, list):
        raise _invalid_response("report data must be a list")
    if any(not isinstance(row, dict) for row in rows):
        raise _invalid_response("report data contains a non-object row")
    return [dict(row) for row in rows]


def _path_value(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise _invalid_response(f"missing required response path: {'.'.join(path)}")
        value = value[key]
    return value


def _retryable(error: DataSourceError) -> bool:
    if error.error_type in {ErrorType.TIMEOUT, ErrorType.NETWORK}:
        return True
    status = error.status_code or 0
    return status == 429 or status >= 500


# provider_health functions removed in v3 — circuit breaker unified into provider_health.py


def provider_health() -> dict[str, Any]:
    """Delegate to the shared provider_health circuit breaker (scheme E unification).

    Previously this read per-module health.json; now it routes through
    the canonical provider_health module
    (\$A_STOCK_STATE_HOME/runtime/provider_health/).
    """
    settings = _settings()
    try:
        import provider_health as ph

        # Build a backward-compatible response from the real SLO ledger
        eastmoney_endpoints = ph.summary().get("providers", {}).get("eastmoney", {})
        circuits = {}
        open_circuits = []
        state = "closed"
        for endpoint_class, score in eastmoney_endpoints.items():
            circuits[endpoint_class] = {
                "state": score.get("state", "closed"),
                "samples": score.get("samples", 0),
                "successes": score.get("successes", 0),
                "success_rate": score.get("success_rate"),
            }
            if score.get("state") in {"open", "half_open"}:
                open_circuits.append(endpoint_class)
                state = score.get("state")
        return {
            "schema": "provider_health_v2",
            "provider": "eastmoney",
            "state": state,
            "open_circuits": sorted(open_circuits),
            "circuits": circuits,
            "coordination_backend": settings.get("coordination_backend", "shared_file"),
            "coordination_root": _coordination_dir(),
        }
    except Exception:  # noqa: BLE001 — graceful fallback
        return {
            "schema": "provider_health_v2",
            "provider": "eastmoney",
            "state": "unknown",
            "open_circuits": [],
            "circuits": {},
            "coordination_backend": settings.get("coordination_backend", "shared_file"),
            "coordination_root": _coordination_dir(),
        }


def _request_payload(
    request,
    *,
    validator: Callable[[Any], Any],
    circuit_key: str = "_",  # kept for caller compatibility; circuit breaker unified into provider_health.py
) -> Any:
    """Execute a provider request with rate limiting, retry, and validation.

    Circuit breaker logic (previously _circuit_before_call / _circuit_record_*)
    has been removed in v3 — all eastmoney endpoints are now governed by the
    shared provider_health module via http_client.py's transport-level
    _record_health() and market_adapters.py's _recorded_call().
    """
    settings = _settings()
    max_attempts = int(settings["max_attempts"])
    last_error: DataSourceError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            _wait_for_provider_slot()
            response = request_json(
                request,
                source="eastmoney",
                timeout=float(settings["timeout_seconds"]),
                max_attempts=1,
            )
            output = validator(response.data)
        except DataSourceError as exc:
            last_error = exc
            if attempt < max_attempts and _retryable(exc):
                delay = (
                    exc.retry_after_seconds
                    if exc.retry_after_seconds is not None
                    else (
                        float(settings["backoff_base_seconds"])
                        * (2 ** (attempt - 1))
                        + random.uniform(0, float(settings["jitter_max_seconds"]))
                    )
                )
                time.sleep(delay)
                continue
            raise DataSourceError(
                "eastmoney",
                exc.message,
                exc.original,
                error_type=exc.error_type,
                attempts=attempt,
                timestamp=exc.timestamp,
                status_code=exc.status_code,
                retry_after_seconds=exc.retry_after_seconds,
            ) from exc
        return output
    if last_error is None:
        raise RuntimeError("Eastmoney request failed without an error")
    raise last_error


def eastmoney_json(
    url: str,
    *,
    required_path: tuple[str, ...] | None = None,
    required_type: type | tuple[type, ...] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fetch a non-datacenter endpoint through the same provider guards."""

    def validate(payload: Any) -> dict[str, Any]:
        checked = _business_status(payload)
        if required_path:
            value = _path_value(checked, required_path)
            if required_type is not None and not isinstance(value, required_type):
                raise _invalid_response(
                    f"response path {'.'.join(required_path)} has invalid type"
                )
        return checked

    parsed = urlparse(url)
    return _request_payload(
        build_request(
            url,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
        ),
        validator=validate,
        circuit_key=f"json:{parsed.netloc}{parsed.path}",
    )


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "", "-") else default
    except (TypeError, ValueError):
        return default


def _day(value: Any) -> str:
    return str(value or "")[:10]


def _code(value: str) -> str:
    raw = str(value or "").strip().lower()
    if "." in raw:
        raw = raw.split(".", 1)[0]
    if raw.startswith(("sh", "sz", "bj")):
        raw = raw[2:]
    return raw.zfill(6)


def _market_suffix(code: str) -> str:
    if code.startswith("6"):
        return "SH"
    if code.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def datacenter_query(
    report_name: str,
    *,
    columns: str = "ALL",
    filter_str: str = "",
    page_size: int = 50,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> list[dict[str, Any]]:
    params = urlencode({
        "reportName": report_name,
        "columns": columns,
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    })
    return _request_payload(
        build_request(
            f"{DATACENTER_URL}?{params}",
            headers={"User-Agent": USER_AGENT, "Referer": "https://data.eastmoney.com/"},
        ),
        validator=_validate_datacenter,
        circuit_key=f"datacenter:{report_name}",
    )


def fetch_lockups(
    code: str,
    *,
    asof: date | str | None = None,
    forward_days: int = 90,
) -> dict[str, list[dict[str, Any]]]:
    normalized = _code(code)
    current = date.fromisoformat(str(asof or date.today())[:10])
    end = current + timedelta(days=forward_days)
    base_filter = f'(SECURITY_CODE="{normalized}")'
    history_data = datacenter_query(
        "RPT_LIFT_STAGE",
        filter_str=base_filter,
        page_size=15,
        sort_columns="FREE_DATE",
        sort_types="-1",
    )
    upcoming_data = datacenter_query(
        "RPT_LIFT_STAGE",
        filter_str=(
            f"{base_filter}(FREE_DATE>='{current.isoformat()}')"
            f"(FREE_DATE<='{end.isoformat()}')"
        ),
        page_size=20,
        sort_columns="FREE_DATE",
        sort_types="1",
    )

    def normalize(row: dict[str, Any]) -> dict[str, Any]:
        ratio = _number(row.get("FREE_RATIO"))
        if 0 < abs(ratio) <= 1:
            ratio *= 100
        current_free_shares = _number(
            row.get("CURRENT_FREE_SHARES")
            if row.get("CURRENT_FREE_SHARES") not in (None, "")
            else row.get("FREE_SHARES_NUM")
        )
        if row.get("CURRENT_FREE_SHARES") not in (None, ""):
            current_free_shares *= 10000
        return {
            "date": _day(row.get("FREE_DATE")),
            "type": str(
                row.get("FREE_SHARES_TYPE")
                or row.get("LIMITED_STOCK_TYPE")
                or ""
            ),
            "shares": current_free_shares,
            "ratio_pct": round(ratio, 4),
        }

    return {
        "history": [normalize(row) for row in history_data],
        "upcoming": [normalize(row) for row in upcoming_data],
    }


def fetch_margin_trading(code: str, page_size: int = 30) -> list[dict[str, Any]]:
    rows = datacenter_query(
        "RPTA_WEB_RZRQ_GGMX",
        filter_str=f'(SCODE="{_code(code)}")',
        page_size=page_size,
        sort_columns="DATE",
        sort_types="-1",
    )
    return [{
        "date": _day(row.get("DATE")),
        "financing_balance": _number(row.get("RZYE")),
        "financing_buy": _number(row.get("RZMRE")),
        "financing_repay": _number(row.get("RZCHE")),
        "securities_balance": _number(row.get("RQYE")),
        "securities_sell_volume": _number(row.get("RQMCL")),
        "securities_repay_volume": _number(row.get("RQCHL")),
        "margin_balance": _number(row.get("RZRQYE")),
    } for row in rows]


def fetch_holder_changes(code: str, page_size: int = 10) -> list[dict[str, Any]]:
    rows = datacenter_query(
        "RPT_HOLDERNUMLATEST",
        filter_str=f'(SECURITY_CODE="{_code(code)}")',
        page_size=page_size,
        sort_columns="END_DATE",
        sort_types="-1",
    )
    return [{
        "date": _day(row.get("END_DATE")),
        "holder_count": _number(row.get("HOLDER_NUM")),
        "holder_change": _number(row.get("HOLDER_NUM_CHANGE")),
        "holder_change_pct": _number(row.get("HOLDER_NUM_RATIO")),
        "average_free_shares": _number(row.get("AVG_FREE_SHARES")),
    } for row in rows]


def fetch_block_trades(code: str, page_size: int = 20) -> list[dict[str, Any]]:
    rows = datacenter_query(
        "RPT_DATA_BLOCKTRADE",
        filter_str=f'(SECURITY_CODE="{_code(code)}")',
        page_size=page_size,
        sort_columns="TRADE_DATE",
        sort_types="-1",
    )
    output = []
    for row in rows:
        close = _number(row.get("CLOSE_PRICE"))
        price = _number(row.get("DEAL_PRICE"))
        output.append({
            "date": _day(row.get("TRADE_DATE")),
            "price": price,
            "close": close,
            "premium_pct": round((price / close - 1) * 100, 2) if close else 0.0,
            "volume": _number(row.get("DEAL_VOLUME")),
            "amount": _number(row.get("DEAL_AMT")),
            "buyer": str(row.get("BUYER_NAME") or ""),
            "seller": str(row.get("SELLER_NAME") or ""),
        })
    return output


def fetch_dragon_tiger(
    code: str,
    *,
    asof: date | str | None = None,
    look_back_days: int = 30,
) -> dict[str, Any]:
    normalized = _code(code)
    current = date.fromisoformat(str(asof or date.today())[:10])
    start = current - timedelta(days=look_back_days)
    records_data = datacenter_query(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=(
            f"(TRADE_DATE>='{start.isoformat()}')"
            f"(TRADE_DATE<='{current.isoformat()}')"
            f'(SECURITY_CODE="{normalized}")'
        ),
        page_size=50,
        sort_columns="TRADE_DATE",
        sort_types="-1",
    )
    records = [{
        "date": _day(row.get("TRADE_DATE")),
        "reason": str(row.get("EXPLANATION") or ""),
        "net_buy_wan": round(_number(row.get("BILLBOARD_NET_AMT")) / 10000, 1),
        "turnover_pct": round(_number(row.get("TURNOVERRATE")), 2),
    } for row in records_data]
    if not records:
        return {
            "records": [],
            "seats": {"buy": [], "sell": []},
            "institution": {
                "buy_amount_wan": 0.0,
                "sell_amount_wan": 0.0,
                "net_amount_wan": 0.0,
            },
        }

    latest = records[0]["date"]
    raw_seats = {}
    for side, report_name, sort_column in (
        ("buy", "RPT_BILLBOARD_DAILYDETAILSBUY", "BUY"),
        ("sell", "RPT_BILLBOARD_DAILYDETAILSSELL", "SELL"),
    ):
        raw_seats[side] = datacenter_query(
            report_name,
            filter_str=f"(TRADE_DATE='{latest}')(SECURITY_CODE=\"{normalized}\")",
            page_size=10,
            sort_columns=sort_column,
            sort_types="-1",
        )
    seats = {
        side: [{
            "name": str(row.get("OPERATEDEPT_NAME") or ""),
            "buy_amount_wan": round(_number(row.get("BUY")) / 10000, 1),
            "sell_amount_wan": round(_number(row.get("SELL")) / 10000, 1),
            "net_amount_wan": round(_number(row.get("NET")) / 10000, 1),
            "institutional": str(row.get("OPERATEDEPT_CODE") or "") == "0",
        } for row in rows[:5]]
        for side, rows in raw_seats.items()
    }
    institution_buy = sum(
        _number(row.get("BUY"))
        for row in raw_seats["buy"]
        if str(row.get("OPERATEDEPT_CODE") or "") == "0"
    ) / 10000
    institution_sell = sum(
        _number(row.get("SELL"))
        for row in raw_seats["sell"]
        if str(row.get("OPERATEDEPT_CODE") or "") == "0"
    ) / 10000
    return {
        "records": records,
        "seats": seats,
        "institution": {
            "buy_amount_wan": round(institution_buy, 1),
            "sell_amount_wan": round(institution_sell, 1),
            "net_amount_wan": round(institution_buy - institution_sell, 1),
        },
    }


def fetch_reports(code: str, page_size: int = 30) -> list[dict[str, Any]]:
    params = urlencode({
        "industryCode": "*",
        "pageSize": str(page_size),
        "industry": "*",
        "rating": "*",
        "ratingChange": "*",
        "beginTime": "2000-01-01",
        "endTime": "2030-01-01",
        "pageNo": "1",
        "fields": "",
        "qType": "0",
        "orgCode": "",
        "code": _code(code),
        "rcode": "",
        "p": "1",
        "pageNum": "1",
        "pageNumber": "1",
    })
    rows = _request_payload(
        build_request(
            f"{REPORT_API_URL}?{params}",
            headers={"User-Agent": USER_AGENT, "Referer": "https://data.eastmoney.com/"},
        ),
        validator=_validate_reports,
        circuit_key="reports",
    )
    return [{
        "date": _day(row.get("publishDate")),
        "title": str(row.get("title") or ""),
        "institution": str(row.get("orgSName") or ""),
        "rating": str(row.get("emRatingName") or ""),
        "industry": str(row.get("indvInduName") or ""),
        "info_code": str(row.get("infoCode") or ""),
        "pdf_url": (
            f"https://pdf.dfcfw.com/pdf/H3_{row.get('infoCode')}_1.pdf"
            if row.get("infoCode") else None
        ),
        "eps_current_year": _number(row.get("predictThisYearEps"), default=0.0),
        "eps_next_year": _number(row.get("predictNextYearEps"), default=0.0),
        "eps_year_after_next": _number(row.get("predictNextTwoYearEps"), default=0.0),
    } for row in rows]


def extract_consensus_eps(code: str, *, asof: date | str | None = None) -> dict[str, Any]:
    """提取个股的一致预期EPS（从东财研报中聚合机构预测）。

    返回当前年度和次年的EPS中位数/平均值/机构覆盖数，
    是四维评分基本面维度的前瞻性输入。

    Returns:
        dict with keys:
            eps_consensus_current: float — 当年EPS一致预期（中位数）
            eps_consensus_next: float — 次年EPS一致预期（中位数）
            eps_consensus_after_next: float — 后年EPS一致预期（中位数）
            eps_mean_current: float — 当年EPS均值
            eps_mean_next: float — 次年EPS均值
            coverage_count: int — 覆盖机构数
            latest_report_date: str — 最新研报日期
            provider: str — 数据源标识
    """
    try:
        reports = fetch_reports(code, asof=asof)
    except Exception:  # noqa: BLE001 — graceful fallback
        return {
            "eps_consensus_current": None,
            "eps_consensus_next": None,
            "eps_consensus_after_next": None,
            "eps_mean_current": None,
            "eps_mean_next": None,
            "coverage_count": 0,
            "latest_report_date": None,
            "provider": "eastmoney_intelligence",
        }

    eps_current = [
        r["eps_current_year"] for r in reports
        if isinstance(r.get("eps_current_year"), (int, float)) and r["eps_current_year"] > 0
    ]
    eps_next = [
        r["eps_next_year"] for r in reports
        if isinstance(r.get("eps_next_year"), (int, float)) and r["eps_next_year"] > 0
    ]
    eps_after_next = [
        r["eps_year_after_next"] for r in reports
        if isinstance(r.get("eps_year_after_next"), (int, float)) and r["eps_year_after_next"] > 0
    ]

    def _median(values: list[float]) -> float | None:
        if not values:
            return None
        sorted_v = sorted(values)
        mid = len(sorted_v) // 2
        if len(sorted_v) % 2 == 0:
            return round((sorted_v[mid - 1] + sorted_v[mid]) / 2, 4)
        return round(sorted_v[mid], 4)

    latest_date = None
    for r in reports:
        rd = r.get("date")
        if rd and (latest_date is None or str(rd) > str(latest_date)):
            latest_date = str(rd)[:10]

    return {
        "eps_consensus_current": _median(eps_current),
        "eps_consensus_next": _median(eps_next),
        "eps_consensus_after_next": _median(eps_after_next),
        "eps_mean_current": round(sum(eps_current) / len(eps_current), 4) if eps_current else None,
        "eps_mean_next": round(sum(eps_next) / len(eps_next), 4) if eps_next else None,
        "coverage_count": len(set(r.get("institution", "") for r in reports if r.get("institution"))),
        "latest_report_date": latest_date,
        "provider": "eastmoney_intelligence",
    }


def fetch_dividend(
    code: str,
    *,
    asof: date | str | None = None,
) -> dict[str, Any] | None:
    normalized = _code(code)
    market = _market_suffix(normalized)
    rows = datacenter_query(
        "RPT_SHAREBONUS_DET",
        filter_str=f'(SECUCODE="{normalized}.{market}")',
        page_size=1,
        sort_columns="PLAN_NOTICE_DATE",
        sort_types="-1",
    )
    if not rows:
        return None
    item = rows[0]
    bonus_per_10 = _number(item.get("PRETAX_BONUS_RMB"))
    if bonus_per_10 <= 0:
        return None
    current = date.fromisoformat(str(asof or date.today())[:10])
    ex_date = _day(item.get("EX_DIVIDEND_DATE"))
    return {
        "bonus_per_10": bonus_per_10,
        "ex_date": ex_date,
        "reg_date": _day(item.get("EQUITY_RECORD_DATE")),
        "plan_date": _day(item.get("PLAN_NOTICE_DATE")),
        "progress": str(item.get("ASSIGN_PROGRESS") or ""),
        "is_upcoming": bool(ex_date and ex_date >= current.isoformat()),
    }


def fetch_research_visits(code: str, page_size: int = 5) -> list[dict[str, Any]]:
    normalized = _code(code)
    market = _market_suffix(normalized)
    rows = datacenter_query(
        "RPT_ORG_SURVEY",
        filter_str=f'(SECUCODE="{normalized}.{market}")',
        page_size=page_size,
        sort_columns="NOTICE_DATE",
        sort_types="-1",
    )
    return [{
        "date": _day(item.get("NOTICE_DATE")),
        "org_count": _number(item.get("RECEPTIONAMOUNT")),
        "summary": str(item.get("MAINPOINT") or "")[:80],
    } for item in rows[:page_size]]


def fetch_insider_trades(code: str, page_size: int = 5) -> list[dict[str, Any]]:
    normalized = _code(code)
    market = _market_suffix(normalized)
    # RPT_HOLDER_TRADE_STOCK was removed by Eastmoney (2026-06).
    # Gracefully return empty instead of letting the error trigger circuit breaker.
    try:
        rows = datacenter_query(
            "RPT_HOLDER_TRADE_STOCK",
            filter_str=f'(SECUCODE="{normalized}.{market}")',
            page_size=page_size,
            sort_columns="NOTICE_DATE",
            sort_types="-1",
        )
    except DataSourceError as exc:
        if exc.status_code == 9501:
            return []
        raise
    return [{
        "date": _day(item.get("NOTICE_DATE")),
        "name": str(item.get("PARTICIPANTNAME") or ""),
        "direction": "增持" if str(item.get("TRADETYPE") or "") == "1" else "减持",
        "shares": _number(item.get("TRADENUM")),
    } for item in rows[:page_size]]


def source_metadata() -> dict[str, Any]:
    return {
        "provider": "eastmoney",
        "adapter_version": ADAPTER_VERSION,
        "upstream_reference": UPSTREAM_VERSION,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "health": provider_health(),
    }
