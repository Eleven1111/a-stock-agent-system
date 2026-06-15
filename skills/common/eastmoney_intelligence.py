"""Normalized Eastmoney adapters for institutional and chip intelligence.

Endpoint selection and field mappings are derived from simonlin1212/a-stock-data
3.2.2 (Apache-2.0), then adapted to this project's urllib transport, shared
configuration, typed failures, and cross-process rate limiting.
"""

from __future__ import annotations

import json
import os
import random
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from data_access_config import provider_settings
from http_client import build_request, request_json
from paths import cache_dir
from state_store import file_lock


DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_API_URL = "https://reportapi.eastmoney.com/report/list"
USER_AGENT = "Mozilla/5.0 (A-Stock-Agent; Eastmoney adapter)"
ADAPTER_VERSION = "eastmoney-intelligence-v1"
UPSTREAM_VERSION = "simonlin1212/a-stock-data@9379ab9"
MIN_INTERVAL_SECONDS = 1.1


def _rate_limit_file() -> str:
    return os.path.join(
        cache_dir("stock-triage"),
        "provider_rate_limits",
        "eastmoney.json",
    )


def _wait_for_provider_slot(
    *,
    minimum_interval: float = MIN_INTERVAL_SECONDS,
    jitter_max: float = 0.25,
) -> None:
    """Serialize Eastmoney calls across local Hermes/OpenClaw processes."""
    path = _rate_limit_file()
    with file_lock(path, timeout=30):
        try:
            with open(path, encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError):
            state = {}
        now = time.time()
        wait = minimum_interval - (now - float(state.get("last_call_epoch") or 0))
        if wait > 0:
            time.sleep(wait + random.uniform(0, max(0.0, jitter_max)))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema": "provider_rate_limit_v1",
                    "provider": "eastmoney",
                    "last_call_epoch": time.time(),
                },
                handle,
            )


def _settings() -> dict[str, Any]:
    return provider_settings("eastmoney")


def _rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    rows = result.get("data") if isinstance(result, dict) else None
    return [dict(row) for row in rows] if isinstance(rows, list) else []


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
    _wait_for_provider_slot()
    settings = _settings()
    response = request_json(
        build_request(
            f"{DATACENTER_URL}?{params}",
            headers={"User-Agent": USER_AGENT, "Referer": "https://data.eastmoney.com/"},
        ),
        source="eastmoney",
        timeout=float(settings["timeout_seconds"]),
        max_attempts=int(settings["max_attempts"]),
    )
    return _rows(response.data)


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
    _wait_for_provider_slot()
    settings = _settings()
    response = request_json(
        build_request(
            f"{REPORT_API_URL}?{params}",
            headers={"User-Agent": USER_AGENT, "Referer": "https://data.eastmoney.com/"},
        ),
        source="eastmoney",
        timeout=float(settings["timeout_seconds"]),
        max_attempts=int(settings["max_attempts"]),
    )
    rows = response.data.get("data") if isinstance(response.data, dict) else None
    if not isinstance(rows, list):
        return []
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


def source_metadata() -> dict[str, str]:
    return {
        "provider": "eastmoney",
        "adapter_version": ADAPTER_VERSION,
        "upstream_reference": UPSTREAM_VERSION,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
