"""Investor interactive Q&A adapters (SZSE Irm.cninfo, SSE sns.sseinfo).

Two exchanges, two adapters, two reliability tiers:

- Shenzhen (互动易, ``irm.cninfo.com.cn``): the public JSON search endpoint
  does not accept a per-stock server-side filter (verified against the live
  endpoint; every parameter name tried returns the same global recency feed).
  We query the feed and filter client-side by ``stockCode``. A request that
  succeeds but yields no rows for the target code is ``status=empty``, not a
  failure — the upstream call worked, the stock just has no recent items in
  the fetched window.
- Shanghai (上证e互动, ``sns.sseinfo.com``): there is no documented per-code
  query either. The company page must be scraped for an internal ``uid``
  (opaque, not derivable from the stock code) before the feed AJAX endpoint
  can be called. This is inherently best-effort HTML scraping; any failure
  degrades to ``sse_unavailable`` rather than raising, honoring the PR
  requirement that Shanghai coverage does not block Shenzhen capability.

Every fetch failure that is NOT the documented best-effort SSE path raises
``DataSourceError`` so callers fail closed instead of caching an empty list
that would be misread as "no Q&A activity".
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode

from data_access_config import provider_settings
from http_client import DataSourceError, ErrorType, build_request, request_json, request_text


ADAPTER_VERSION = "interactive-qa-v1"

SZSE_SEARCH_URL = "http://irm.cninfo.com.cn/newircs/index/search"
SSE_COMPANY_URL = "https://sns.sseinfo.com/company.do"
SSE_FEED_URL = "https://sns.sseinfo.com/ajax/userfeeds.do"
USER_AGENT = "Mozilla/5.0 (A-Stock-Agent; interactive-qa adapter)"

DEFAULT_RETENTION = 10
_SZSE_FEED_PAGE_SIZE = 200


def _code(value: str) -> str:
    raw = str(value or "").strip().lower()
    if "." in raw:
        raw = raw.split(".", 1)[0]
    if raw.startswith(("sh", "sz", "bj")):
        raw = raw[2:]
    return raw.zfill(6)


def _market(code: str) -> str:
    """Route by listing venue. Shanghai main board starts with 6."""
    return "sse" if code.startswith("6") else "szse"


def _day(value: Any) -> str:
    """Normalize an epoch-millis or date-like value to an ISO date string."""
    if value in (None, ""):
        return ""
    try:
        millis = int(value)
        return (
            datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
            .date()
            .isoformat()
        )
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    match = re.match(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return ""
    return text[:10]


def _invalid(source: str, message: str) -> DataSourceError:
    return DataSourceError(source, message, error_type=ErrorType.INVALID_RESPONSE)


def _settings(provider: str) -> dict[str, Any]:
    return provider_settings(provider)


def _szse_query(*, page_size: int) -> Any:
    settings = _settings("cninfo")
    body = json.dumps(
        {"pageNum": 1, "pageSize": page_size, "tabName": "fulltext", "keyWord": ""}
    ).encode("utf-8")
    request = build_request(
        SZSE_SEARCH_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Referer": "https://irm.cninfo.com.cn/",
        },
        method="POST",
    )
    return request_json(
        request,
        source="cninfo",
        timeout=float(settings["timeout_seconds"]),
        max_attempts=int(settings["max_attempts"]),
    ).data


def fetch_szse_interactive_qa(
    code: str,
    *,
    page_size: int = _SZSE_FEED_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Fetch recent Shenzhen 互动易 Q&A for ``code`` (best-effort filter).

    The upstream ``irm.cninfo.com.cn`` search endpoint has no working
    per-stock filter, so this pulls the shared recency feed and filters
    client-side. Raises ``DataSourceError`` only when the HTTP call itself
    fails or the response shape is invalid; a successful call with zero
    matching rows returns an empty list (the caller records that as
    ``status=empty``, not a failure).
    """
    normalized = _code(code)
    payload = _szse_query(page_size=page_size)
    if not isinstance(payload, dict):
        raise _invalid("cninfo", "irm search response root must be an object")
    results = payload.get("results")
    if not isinstance(results, list):
        raise _invalid("cninfo", "irm search response missing results list")

    rows: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if _code(str(item.get("stockCode") or "")) != normalized:
            continue
        question_date = _day(item.get("pubDate"))
        reply = str(item.get("attachedContent") or "").strip()
        reply_date = _day(item.get("attachedPubDate")) if reply else ""
        rows.append({
            "date": reply_date or question_date,
            "question_date": question_date,
            "reply_date": reply_date or None,
            "question": str(item.get("mainContent") or "").strip(),
            "reply": reply or None,
            "has_reply": bool(reply),
            "platform": "szse_irm",
            "company": str(item.get("companyShortName") or ""),
            "url": (
                f"https://irm.cninfo.com.cn/mobile/rmDetail?questionId={item.get('indexId')}"
                if item.get("indexId")
                else None
            ),
        })
    rows.sort(key=lambda row: row.get("date") or "", reverse=True)
    return rows


_SSE_UID_PATTERN = re.compile(r'ajax/userfeeds\.do\?typeCode=company[^"\']*uid=(\d+)')
_SSE_ITEM_PATTERN = re.compile(
    r'<div class="m_feed_item" id="item-(?P<item_id>\d+)">(?P<body>.*?)'
    r'(?=<div class="m_feed_item" id="item-|\Z)',
    re.DOTALL,
)
_SSE_QUESTION_PATTERN = re.compile(
    r'<div class="m_feed_txt">\s*(?:<a[^>]*>.*?</a>)?(?P<question>.*?)</div>',
    re.DOTALL,
)
_SSE_REPLY_PATTERN = re.compile(
    r'<div class="m_feed_txt" id="m_feed_txt-\d+">(?P<reply>.*?)</div>',
    re.DOTALL,
)
_SSE_DATE_PATTERN = re.compile(
    r"(\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2})"
)


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _resolve_sse_uid(code: str) -> str:
    settings = _settings("sse")
    request = build_request(
        f"{SSE_COMPANY_URL}?{urlencode({'stockcode': code})}",
        headers={"User-Agent": USER_AGENT},
    )
    html = request_text(
        request,
        source="sse",
        timeout=float(settings["timeout_seconds"]),
        max_attempts=int(settings["max_attempts"]),
    ).data
    match = _SSE_UID_PATTERN.search(html)
    if not match:
        raise _invalid("sse", f"could not resolve sns.sseinfo.com uid for {code}")
    return match.group(1)


def fetch_sse_interactive_qa(
    code: str,
    *,
    page_size: int = 10,
) -> list[dict[str, Any]]:
    """Best-effort fetch of Shanghai 上证e互动 Q&A for ``code``.

    Requires scraping the company page for an opaque internal ``uid`` before
    the feed AJAX endpoint can be called. Any failure (page shape change,
    network error, missing uid) raises ``DataSourceError``; the caller
    degrades this to an explicit ``sse_unavailable`` marker rather than
    treating it as a hard requirement.
    """
    normalized = _code(code)
    settings = _settings("sse")
    uid = _resolve_sse_uid(normalized)
    request = build_request(
        f"{SSE_FEED_URL}?{urlencode({'typeCode': 'company', 'type': 11, 'pageSize': page_size, 'uid': uid, 'page': 1})}",
        headers={
            "User-Agent": USER_AGENT,
            "Referer": f"{SSE_COMPANY_URL}?stockcode={normalized}",
        },
    )
    html = request_text(
        request,
        source="sse",
        timeout=float(settings["timeout_seconds"]),
        max_attempts=int(settings["max_attempts"]),
    ).data

    rows: list[dict[str, Any]] = []
    for match in _SSE_ITEM_PATTERN.finditer(html):
        item_id = match.group("item_id")
        body = match.group("body")
        question_match = _SSE_QUESTION_PATTERN.search(body)
        reply_match = _SSE_REPLY_PATTERN.search(body)
        dates = _SSE_DATE_PATTERN.findall(body)
        question_date = _day(dates[0]) if dates else ""
        reply_date = _day(dates[1]) if reply_match and len(dates) > 1 else ""
        question = _strip_tags(question_match.group("question")) if question_match else ""
        reply = _strip_tags(reply_match.group("reply")) if reply_match else ""
        if not question and not reply:
            continue
        rows.append({
            "date": reply_date or question_date,
            "question_date": question_date,
            "reply_date": reply_date or None,
            "question": question,
            "reply": reply or None,
            "has_reply": bool(reply),
            "platform": "sse_e_hudong",
            "company": "",
            "url": f"https://sns.sseinfo.com/company.do?stockcode={normalized}#item-{item_id}",
        })
    rows.sort(key=lambda row: row.get("date") or "", reverse=True)
    return rows


def fetch_interactive_qa(
    code: str,
    *,
    asof: date | str | None = None,
    page_size: int = 10,
    retention: int = DEFAULT_RETENTION,
) -> dict[str, Any]:
    """Route by exchange and return a status-tagged Q&A payload.

    Always returns a dict with ``market``, ``status`` and ``rows``. Never
    raises for the Shanghai best-effort path (degrades to
    ``sse_unavailable``); raises ``DataSourceError`` for the Shenzhen path so
    the caller can fail closed like the other required-ish datasets.
    """
    normalized = _code(code)
    market = _market(normalized)
    if market == "sse":
        try:
            rows = fetch_sse_interactive_qa(normalized, page_size=page_size)
        except DataSourceError as exc:
            return {
                "market": "sse",
                "status": "sse_unavailable",
                "rows": [],
                "error": exc.to_dict(),
            }
        return {
            "market": "sse",
            "status": "ok" if rows else "empty",
            "rows": rows[: max(0, int(retention))],
        }
    rows = fetch_szse_interactive_qa(normalized, page_size=_SZSE_FEED_PAGE_SIZE)
    return {
        "market": "szse",
        "status": "ok" if rows else "empty",
        "rows": rows[: max(0, int(retention))],
    }


def source_metadata() -> dict[str, Any]:
    return {
        "provider": "cninfo_sse",
        "adapter_version": ADAPTER_VERSION,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
