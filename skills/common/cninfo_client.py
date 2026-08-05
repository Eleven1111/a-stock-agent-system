"""Shared CNINFO (巨潮资讯网) access layer.

Two access patterns live here:

* per-stock history (:func:`query_announcements`) — used by
  ``announcement_risk`` to gate recommendations;
* whole-market by disclosure day (:func:`fetch_day`) — used by the
  announcement radar to build a daily recall set.

Both go through :mod:`http_client` so the ``cninfo``/``cninfo_bulk``
throttle buckets, retry policy and provider health accounting in
``config/data_access.json`` apply. Never call urllib directly against
CNINFO from business modules.

The whole-market scan issues roughly one request per 30 announcements
(~50 requests for a normal trading day) and is therefore billed to a
separate ``cninfo_bulk`` source so a slow batch scan cannot starve the
latency-sensitive per-stock lookups.
"""

from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache
from typing import Any, Iterator
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

try:
    from .http_client import build_request, request_json
except ImportError:  # pragma: no cover - exercised via PYTHONPATH=skills/common
    from http_client import build_request, request_json


_CN_TZ = ZoneInfo("Asia/Shanghai")

QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
TOP_SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
PDF_BASE = "https://static.cninfo.com.cn/"

SOURCE_STOCK = "cninfo"
SOURCE_BULK = "cninfo_bulk"

# 巨潮的 column 参数：szse=深市, sse=沪市, bj=北交所
MARKET_COLUMNS = ("szse", "sse", "bj")

# 接口单页上限 30，超过会被静默截断
PAGE_SIZE = 30
# 单个 column 的翻页上限。正常交易日单市场 < 40 页；设上限是为了在接口
# 行为异常（hasMore 恒 True）时 fail loud 而不是无限翻页。
MAX_PAGES_PER_COLUMN = 200

_BASE_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": "Mozilla/5.0 A-Stock-Agent",
    "Referer": "https://www.cninfo.com.cn/",
}

_TAG_RE = re.compile(r"<[^>]+>")


class CninfoPaginationError(RuntimeError):
    """Raised when the paging loop exceeds :data:`MAX_PAGES_PER_COLUMN`."""


def clean_title(raw: Any) -> str:
    """Strip the ``<em>`` highlight markup and full-width padding."""
    text = _TAG_RE.sub("", str(raw or ""))
    return text.replace("　", " ").replace("&amp;", "&").strip()


def millis_to_date(value: Any) -> str:
    """Convert a CNINFO epoch-millis stamp to its disclosure date.

    CNINFO stamps are Beijing time, so the conversion is pinned to
    Asia/Shanghai rather than UTC or the host timezone: an announcement
    posted at 00:30 CST is disclosed *that* day, but UTC would file it
    under the previous one, and late-night disclosures are common.
    """
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=_CN_TZ).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def pdf_url(adjunct: Any) -> str:
    adjunct = str(adjunct or "")
    return urljoin(PDF_BASE, adjunct) if adjunct else ""


def _post_query(payload: dict[str, Any], *, source: str, timeout: float) -> dict[str, Any]:
    request = build_request(
        QUERY_URL,
        data=urlencode(payload).encode("utf-8"),
        headers=_BASE_HEADERS,
        method="POST",
    )
    data = request_json(request, source=source, timeout=timeout, encoding="utf-8").data
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=512)
def lookup_org_id(stock_code: str, timeout: int = 8) -> str:
    """Resolve a 6-digit code to CNINFO's internal orgId."""
    code = str(stock_code).zfill(6)
    request = build_request(
        TOP_SEARCH_URL,
        data=urlencode({"keyWord": code, "maxNum": "10"}).encode("utf-8"),
        headers=_BASE_HEADERS,
        method="POST",
    )
    items = request_json(
        request,
        source=SOURCE_STOCK,
        timeout=timeout,
        encoding="utf-8",
    ).data
    match = next(
        (
            item for item in items
            if str(item.get("code") or "").zfill(6) == code and item.get("orgId")
        ),
        None,
    )
    if not match:
        raise LookupError(f"CNINFO orgId not found for {code}")
    return str(match["orgId"])


def query_announcements(
    stock_code: str,
    *,
    page_size: int = 30,
    timeout: int = 8,
) -> list[dict[str, Any]]:
    """Fetch the latest announcements for a single stock (raw API items)."""
    code = str(stock_code).zfill(6)
    market = "szse" if code.startswith(("0", "3")) else "sse"
    org_id = lookup_org_id(code, timeout=timeout)
    payload = {
        "stock": f"{code},{org_id}",
        "searchkey": "",
        "plate": market,
        "category": "",
        "trade": "",
        "column": market,
        "columnTitle": "历史公告查询",
        "pageNum": "1",
        "pageSize": str(page_size),
        "tabName": "fulltext",
        "sortName": "",
        "sortType": "",
        "limit": "",
        "seDate": "",
        "isHLtitle": "true",
    }
    data = _post_query(payload, source=SOURCE_STOCK, timeout=timeout)
    return list(data.get("announcements") or [])


def _iter_column_pages(
    day: str,
    column: str,
    *,
    timeout: float,
) -> Iterator[list[dict[str, Any]]]:
    page = 1
    while True:
        payload = {
            "pageNum": page,
            "pageSize": PAGE_SIZE,
            "column": column,
            "tabName": "fulltext",
            "plate": "",
            "stock": "",
            "searchkey": "",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": f"{day}~{day}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        data = _post_query(payload, source=SOURCE_BULK, timeout=timeout)
        announcements = list(data.get("announcements") or [])
        if not announcements:
            return
        yield announcements

        total_pages = data.get("totalpages") or 0
        if (
            data.get("hasMore") is False
            or (total_pages and page >= total_pages)
            or len(announcements) < PAGE_SIZE
        ):
            return
        page += 1
        if page > MAX_PAGES_PER_COLUMN:
            raise CninfoPaginationError(
                f"{column} {day}: 超过 {MAX_PAGES_PER_COLUMN} 页仍未终止，疑似接口分页异常"
            )


def fetch_day(
    day: str,
    *,
    columns: tuple[str, ...] | list[str] | None = None,
    codes: set[str] | None = None,
    timeout: float = 15,
) -> list[dict[str, Any]]:
    """Fetch every announcement disclosed on *day* (``YYYY-MM-DD``).

    ``codes`` optionally restricts the result to a watchlist of 6-digit
    codes. Announcements are de-duplicated by ``announcementId`` across
    markets. Rows are plain dicts so downstream stages stay I/O-free.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for column in (columns or MARKET_COLUMNS):
        for announcements in _iter_column_pages(day, column, timeout=timeout):
            for item in announcements:
                ann_id = str(item.get("announcementId") or "")
                if not ann_id or ann_id in seen:
                    continue
                seen.add(ann_id)

                code = str(item.get("secCode") or "").strip()
                if codes and code.zfill(6) not in codes:
                    continue

                rows.append({
                    "ann_id": ann_id,
                    "code": code,
                    "name": str(item.get("secName") or "").strip(),
                    "title": clean_title(item.get("announcementTitle")),
                    "ann_date": millis_to_date(item.get("announcementTime")) or day,
                    "sec_type": str(item.get("announcementType") or ""),
                    "board": column,
                    "url": pdf_url(item.get("adjunctUrl")),
                })

    return rows


__all__ = [
    "CninfoPaginationError",
    "MARKET_COLUMNS",
    "PDF_BASE",
    "QUERY_URL",
    "SOURCE_BULK",
    "SOURCE_STOCK",
    "TOP_SEARCH_URL",
    "clean_title",
    "fetch_day",
    "lookup_org_id",
    "millis_to_date",
    "pdf_url",
    "query_announcements",
]
