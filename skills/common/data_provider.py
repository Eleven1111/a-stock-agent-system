"""Shared urllib-based clients for external market and news data."""

from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional

try:
    from .a_stock_http import parse_tencent_quote_line
    from .data_access_config import provider_settings
    from .http_client import DataSourceError, ErrorType, HttpClient, HttpResult, build_request
except ImportError:
    from a_stock_http import parse_tencent_quote_line
    from data_access_config import provider_settings
    from http_client import DataSourceError, ErrorType, HttpClient, HttpResult, build_request

__all__ = [
    "fetch_serpapi_news",
    "fetch_tencent_quote",
    "fetch_tencent_quotes",
    "provider_client",
    "tencent_symbol",
]


def provider_client(source: str) -> HttpClient:
    settings = provider_settings(source)
    return HttpClient(
        source,
        timeout=float(settings.get("timeout_seconds", 10)),
        max_attempts=int(settings.get("max_attempts", 2)),
    )


def tencent_symbol(code: str) -> str:
    normalized = str(code).strip().lower()
    if normalized.startswith(("sh", "sz", "hk")):
        return normalized
    return ("sh" if normalized.startswith("6") else "sz") + normalized.zfill(6)


def fetch_tencent_quotes(
    codes: List[str],
    *,
    client: Optional[HttpClient] = None,
) -> HttpResult[Dict[str, Dict[str, Any]]]:
    symbols = [tencent_symbol(code) for code in codes]
    request = build_request(
        "http://qt.gtimg.cn/q=" + ",".join(symbols),
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response = (client or provider_client("tencent")).request_text(request, encoding="gbk")
    quotes: Dict[str, Dict[str, Any]] = {}
    for line in response.data.strip().splitlines():
        parsed = parse_tencent_quote_line(line)
        if not parsed:
            continue
        quotes[parsed["code"]] = {
            **parsed["fields"],
            "provider": "tencent",
            "fetched_at": response.fetched_at,
        }
    if not quotes:
        raise DataSourceError(
            "tencent",
            "no valid quote records",
            error_type=ErrorType.INVALID_RESPONSE,
            attempts=response.attempts,
            timestamp=response.fetched_at,
        )
    return HttpResult(quotes, response.fetched_at, response.attempts)


def fetch_tencent_quote(
    code: str,
    *,
    client: Optional[HttpClient] = None,
) -> Dict[str, Any]:
    symbol = tencent_symbol(code)
    result = fetch_tencent_quotes([symbol], client=client)
    quote = result.data.get(symbol)
    if quote is None:
        raise DataSourceError(
            "tencent",
            f"quote missing for {symbol}",
            error_type=ErrorType.INVALID_RESPONSE,
            attempts=result.attempts,
            timestamp=result.fetched_at,
        )
    return quote


def fetch_serpapi_news(
    query: str,
    api_key: str,
    limit: int,
    *,
    client: Optional[HttpClient] = None,
) -> HttpResult[List[Dict[str, Any]]]:
    params = urllib.parse.urlencode({
        "engine": "google_news",
        "q": query,
        "hl": "zh-cn",
        "gl": "cn",
        "api_key": api_key,
    })
    request = build_request(
        f"https://serpapi.com/search.json?{params}",
        headers={"User-Agent": "Hermes A-Stock Agent"},
    )
    response = (client or provider_client("serpapi")).request_json(request)
    if not isinstance(response.data, dict):
        raise DataSourceError(
            "serpapi",
            "expected a JSON object",
            error_type=ErrorType.INVALID_RESPONSE,
            attempts=response.attempts,
            timestamp=response.fetched_at,
        )
    items = response.data.get("news_results") or []
    if not isinstance(items, list):
        raise DataSourceError(
            "serpapi",
            "news_results must be a list",
            error_type=ErrorType.INVALID_RESPONSE,
            attempts=response.attempts,
            timestamp=response.fetched_at,
        )
    events = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        snippet = item.get("snippet") or ""
        if not title and not snippet:
            continue
        source = item.get("source")
        events.append({
            "query": query,
            "title": title,
            "snippet": snippet,
            "source": source.get("name") if isinstance(source, dict) else source,
            "date": item.get("date"),
            "link": item.get("link"),
            "provider": "serpapi",
            "fetched_at": response.fetched_at,
        })
    return HttpResult(events, response.fetched_at, response.attempts)
