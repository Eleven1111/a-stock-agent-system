"""Shared urllib-based clients for external market and news data."""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict, List, Optional

try:
    from .a_stock_http import (
        fetch_tencent_quotes_result as _fetch_tencent_quotes_result,
        tencent_symbol as _tencent_symbol,
    )
    from .data_access_config import provider_settings
    from .http_client import DataSourceError, ErrorType, HttpClient, HttpResult, build_request
except ImportError:
    from a_stock_http import (
        fetch_tencent_quotes_result as _fetch_tencent_quotes_result,
        tencent_symbol as _tencent_symbol,
    )
    from data_access_config import provider_settings
    from http_client import DataSourceError, ErrorType, HttpClient, HttpResult, build_request

__all__ = [
    "fetch_serpapi_news",
    "fetch_serper_news",
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
    return _tencent_symbol(code)


def fetch_tencent_quotes(
    codes: List[str],
    *,
    client: Optional[HttpClient] = None,
) -> HttpResult[Dict[str, Dict[str, Any]]]:
    return _fetch_tencent_quotes_result(codes, client=client)


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


def fetch_serper_news(
    query: str,
    api_key: str,
    limit: int,
    *,
    client: Optional[HttpClient] = None,
) -> HttpResult[List[Dict[str, Any]]]:
    request = build_request(
        "https://google.serper.dev/news",
        data=json.dumps({"q": query, "gl": "cn", "hl": "zh-cn", "num": max(1, int(limit))}).encode("utf-8"),
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    response = (client or provider_client("serper")).request_json(request)
    if not isinstance(response.data, dict):
        raise DataSourceError(
            "serper",
            "expected a JSON object",
            error_type=ErrorType.INVALID_RESPONSE,
            attempts=response.attempts,
            timestamp=response.fetched_at,
        )
    items = response.data.get("news") or []
    if not isinstance(items, list):
        raise DataSourceError(
            "serper",
            "news must be a list",
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
        events.append({
            "query": query,
            "title": title,
            "snippet": snippet,
            "source": item.get("source"),
            "date": item.get("date"),
            "link": item.get("link"),
            "provider": "serper",
            "fetched_at": response.fetched_at,
        })
    return HttpResult(events, response.fetched_at, response.attempts)
