"""Best-effort CNINFO announcement title scan for recommendation gating."""

from __future__ import annotations

import io
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterable
from urllib.parse import urlencode, urljoin

try:
    from .http_client import build_request, request_bytes, request_json
except ImportError:
    from http_client import build_request, request_bytes, request_json


QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
TOP_SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
PDF_BASE = "https://static.cninfo.com.cn/"
RISK_TITLE_TERMS = ("澄清", "异常波动", "风险提示", "问询", "监管", "更正")


@lru_cache(maxsize=512)
def lookup_org_id(stock_code: str, timeout: int = 8) -> str:
    code = str(stock_code).zfill(6)
    request = build_request(
        TOP_SEARCH_URL,
        data=urlencode({"keyWord": code, "maxNum": "10"}).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "Mozilla/5.0 A-Stock-Agent",
            "Referer": "https://www.cninfo.com.cn/",
        },
        method="POST",
    )
    items = request_json(
        request,
        source="cninfo",
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


def _millis_to_date(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _extract_pdf_text(url: str, timeout: int = 8, max_pages: int = 5) -> str:
    if not url:
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    request = build_request(
        url,
        headers={"User-Agent": "Mozilla/5.0 A-Stock-Agent"},
    )
    try:
        payload = request_bytes(
            request,
            source="cninfo",
            timeout=timeout,
        ).data[:6 * 1024 * 1024]
        reader = PdfReader(io.BytesIO(payload))
        return "\n".join(
            (page.extract_text() or "")
            for page in reader.pages[:max_pages]
        )[:20000]
    except Exception:
        return ""


def fetch_announcements(stock_code: str, page_size: int = 30, timeout: int = 8) -> list[dict[str, Any]]:
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
    request = build_request(
        QUERY_URL,
        data=urlencode(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "Mozilla/5.0 A-Stock-Agent",
            "Referer": "https://www.cninfo.com.cn/",
        },
        method="POST",
    )
    data = request_json(
        request,
        source="cninfo",
        timeout=timeout,
        encoding="utf-8",
    ).data
    result = []
    enriched = 0
    for item in (data.get("announcements") or []):
        title = re.sub(r"<[^>]+>", "", str(item.get("announcementTitle") or ""))
        adjunct = str(item.get("adjunctUrl") or "")
        url = urljoin(PDF_BASE, adjunct) if adjunct else ""
        text = ""
        if enriched < 1 and any(term in title for term in RISK_TITLE_TERMS):
            text = _extract_pdf_text(url, timeout=min(timeout, 4))
            enriched += 1
        result.append({
            "title": title,
            "date": _millis_to_date(item.get("announcementTime")),
            "source": "CNINFO",
            "url": url,
            "text": text,
        })
    return result


def scan_many(codes: Iterable[str], timeout: int = 8) -> dict[str, list[dict[str, Any]] | None]:
    unique = list(dict.fromkeys(str(code)[-6:].zfill(6) for code in codes if code))

    def _fetch(code: str) -> tuple[str, list[dict[str, Any]] | None]:
        try:
            return code, fetch_announcements(code, timeout=timeout)
        except Exception:
            return code, None

    with ThreadPoolExecutor(max_workers=min(5, len(unique) or 1)) as pool:
        return dict(pool.map(_fetch, unique))
