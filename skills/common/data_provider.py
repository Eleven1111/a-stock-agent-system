"""Shared urllib-based clients for external market and news data."""

from __future__ import annotations

import json
import os
import threading
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


# ─── serper.dev multi-key rotation ───

_SERPER_KEY_INDEX = 0
_SERPER_KEY_LOCK = threading.Lock()


def _serper_keys() -> List[str]:
    """Load all serper.dev API keys from env (comma-separated or single)."""
    raw = os.environ.get("SERPER_API_KEYS") or os.environ.get("SERPER_API_KEY") or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


def _next_serper_key() -> str:
    """Round-robin key selection. Returns empty string if none configured."""
    global _SERPER_KEY_INDEX
    keys = _serper_keys()
    if not keys:
        return ""
    with _SERPER_KEY_LOCK:
        key = keys[_SERPER_KEY_INDEX % len(keys)]
        _SERPER_KEY_INDEX += 1
    return key


def fetch_serper_news(
    query: str,
    api_key: Optional[str] = None,
    limit: int = 5,
    *,
    client: Optional[HttpClient] = None,
) -> HttpResult[List[Dict[str, Any]]]:
    """Fetch news via serper.dev with automatic multi-key rotation.

    If *api_key* is provided, uses it directly. Otherwise picks the next key
    from the round-robin pool (SERPER_API_KEYS / SERPER_API_KEY env var).
    On 429/403 errors, retries with the next available key.
    """
    keys = [api_key] if api_key else _serper_keys()
    if not keys:
        raise DataSourceError(
            "serper",
            "SERPER_API_KEY not configured",
            error_type=ErrorType.UNKNOWN,
            attempts=0,
            timestamp="",
        )

    last_error: Optional[Exception] = None
    for attempt, key in enumerate(keys):
        request = build_request(
            "https://google.serper.dev/news",
            data=json.dumps({"q": query, "gl": "cn", "hl": "zh-cn", "num": max(1, int(limit))}).encode("utf-8"),
            headers={
                "X-API-KEY": key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            response = (client or provider_client("serper")).request_json(request)
        except DataSourceError as exc:
            last_error = exc
            # 429 = rate limit, 403 = invalid key → try next key
            if attempt < len(keys) - 1:
                continue
            raise
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

    raise last_error or DataSourceError(
        "serper", "all keys exhausted", error_type=ErrorType.MISSING_KEY, attempts=len(keys), timestamp="",
    )
