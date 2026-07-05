"""Unified multi-provider web search adapter.

Gives research_bus tasks and serenity source harvesting a fail-closed web
search primitive that does not depend on the calling agent session's own
browsing tool. Three provider backends are supported, all routed through
``http_client`` so every physical request is recorded by
``provider_health``:

- **Tavily** (``TAVILY_API_KEYS``): ``POST https://api.tavily.com/search``.
- **Bocha 博查** (``BOCHA_API_KEYS``): ``POST https://api.bochaai.com/v1/web-search``.
- **SearXNG self-hosted** (``SEARXNG_BASE_URLS``): ``GET {base}/search``.

Each provider name maps to an env var holding a comma-separated key/URL pool.
A provider with no configured pool is skipped entirely (not attempted, not
counted as a failure) so a deployment with only one provider configured never
reports spurious errors for the other two.

Degrade chain: providers are tried in order (default
``tavily -> bocha -> searxng``, overridable via ``config/web_search.json``'s
``provider_order``) until one returns a result. Within a single provider, its
key/URL pool is tried in order; HTTP 401/402/429 mark that key unusable for
this call and the next key in the pool is tried. A provider is exhausted only
when every key in its pool has failed.

Fail-closed: if every configured provider fails, ``status`` is
``all_failed`` with an ``errors`` list — never an empty ``items`` list
pretending to be a clean zero-result search. If nothing is configured at
all, ``status`` is ``disabled``. A provider that produced zero results is
reported as ``status=empty`` (still a real, successful call).

Secrets never appear in the returned payload: errors carry only the
provider name, HTTP status, and a generic message.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from .http_client import DataSourceError, build_request, request_json
except ImportError:  # pragma: no cover - script-style sys.path imports
    from http_client import DataSourceError, build_request, request_json  # type: ignore

try:
    from . import provider_health
except ImportError:  # pragma: no cover - script-style sys.path imports
    import provider_health  # type: ignore


__all__ = ["search", "main"]

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_CONFIG_PATH = os.path.join(_REPO_ROOT, "config", "web_search.json")

DEFAULT_PROVIDER_ORDER = ["tavily", "bocha", "searxng"]
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESULTS = 8

# HTTP statuses that mean "this key is unusable", not "the provider is down":
# quota exhausted (429), payment required (402), or invalid credential (401).
KEY_ROTATION_STATUS_CODES = {401, 402, 429}


def _load_config(path: str | None = None) -> dict[str, Any]:
    config_path = path or DEFAULT_CONFIG_PATH
    try:
        with open(config_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return payload


def _provider_order(config: dict[str, Any]) -> list[str]:
    order = config.get("provider_order")
    if isinstance(order, list) and order:
        cleaned = [str(item) for item in order if isinstance(item, str) and item.strip()]
        if cleaned:
            return cleaned
    return list(DEFAULT_PROVIDER_ORDER)


def _provider_setting(config: dict[str, Any], provider: str, key: str, default: Any) -> Any:
    providers_cfg = config.get("providers")
    providers_cfg = providers_cfg if isinstance(providers_cfg, dict) else {}
    provider_cfg = providers_cfg.get(provider)
    provider_cfg = provider_cfg if isinstance(provider_cfg, dict) else {}
    value = provider_cfg.get(key)
    return value if value is not None else default


def _split_pool(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _tavily_pool() -> list[str]:
    raw = os.environ.get("TAVILY_API_KEYS") or os.environ.get("TAVILY_API_KEY") or ""
    return _split_pool(raw)


def _bocha_pool() -> list[str]:
    raw = os.environ.get("BOCHA_API_KEYS") or os.environ.get("BOCHA_API_KEY") or ""
    return _split_pool(raw)


def _searxng_pool() -> list[str]:
    raw = os.environ.get("SEARXNG_BASE_URLS") or os.environ.get("SEARXNG_BASE_URL") or ""
    return [item.rstrip("/") for item in _split_pool(raw)]


_PROVIDER_POOLS = {
    "tavily": _tavily_pool,
    "bocha": _bocha_pool,
    "searxng": _searxng_pool,
}


def _sanitized_error(provider: str, exc: DataSourceError) -> dict[str, Any]:
    """Error detail with no key/URL material — status code + generic message only."""
    return {
        "provider": provider,
        "error_type": exc.error_type,
        "status_code": exc.status_code,
        "message": f"{provider} request failed",
    }


def _record_health(provider: str, ok: bool) -> None:
    try:
        provider_health.record_result(provider, "web_search", ok)
    except Exception:  # noqa: BLE001 - health bookkeeping never breaks a search call
        pass


def _parse_published(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _mark_freshness(
    items: list[dict[str, Any]],
    *,
    freshness_days: float | None,
    now: datetime,
) -> list[dict[str, Any]]:
    if freshness_days is None:
        for item in items:
            item["stale"] = False
        return items
    floor = now - timedelta(days=float(freshness_days))
    for item in items:
        published = _parse_published(item.get("published"))
        item["stale"] = bool(published is not None and published < floor)
    return items


def _normalize_tavily(payload: Any, provider: str) -> list[dict[str, Any]]:
    results = payload.get("results") if isinstance(payload, dict) else None
    results = results if isinstance(results, list) else []
    items: list[dict[str, Any]] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not title or not url:
            continue
        items.append({
            "title": title,
            "url": url,
            "snippet": str(entry.get("content") or "").strip(),
            "published": entry.get("published_date"),
            "provider": provider,
        })
    return items


def _normalize_bocha(payload: Any, provider: str) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    web_pages = data.get("webPages") if isinstance(data, dict) else None
    values = web_pages.get("value") if isinstance(web_pages, dict) else None
    values = values if isinstance(values, list) else []
    items: list[dict[str, Any]] = []
    for entry in values:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not title or not url:
            continue
        items.append({
            "title": title,
            "url": url,
            "snippet": str(entry.get("snippet") or "").strip(),
            "published": entry.get("datePublished"),
            "provider": provider,
        })
    return items


def _normalize_searxng(payload: Any, provider: str) -> list[dict[str, Any]]:
    results = payload.get("results") if isinstance(payload, dict) else None
    results = results if isinstance(results, list) else []
    items: list[dict[str, Any]] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not title or not url:
            continue
        items.append({
            "title": title,
            "url": url,
            "snippet": str(entry.get("content") or "").strip(),
            "published": entry.get("publishedDate"),
            "provider": provider,
        })
    return items


def _call_tavily(key: str, query: str, *, max_results: int, timeout: float) -> Any:
    body = json.dumps({
        "api_key": key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
    }).encode("utf-8")
    request = build_request(
        "https://api.tavily.com/search",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    result = request_json(request, source="tavily", timeout=timeout, max_attempts=1)
    return result.data


def _call_bocha(key: str, query: str, *, max_results: int, timeout: float) -> Any:
    body = json.dumps({
        "query": query,
        "count": max_results,
        "summary": True,
    }).encode("utf-8")
    request = build_request(
        "https://api.bochaai.com/v1/web-search",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    result = request_json(request, source="bocha", timeout=timeout, max_attempts=1)
    return result.data


def _call_searxng(base_url: str, query: str, *, max_results: int, timeout: float) -> Any:
    from urllib.parse import quote

    url = f"{base_url}/search?q={quote(query)}&format=json"
    request = build_request(url, method="GET")
    result = request_json(request, source="searxng", timeout=timeout, max_attempts=1)
    return result.data


_PROVIDER_CALLS = {
    "tavily": (_call_tavily, _normalize_tavily),
    "bocha": (_call_bocha, _normalize_bocha),
    "searxng": (_call_searxng, _normalize_searxng),
}


def _run_provider(
    provider: str,
    query: str,
    *,
    max_results: int,
    timeout: float,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Try every key/URL in the provider's pool. Returns (items, error)."""
    pool_fn = _PROVIDER_POOLS.get(provider)
    call_fn, normalize_fn = _PROVIDER_CALLS.get(provider, (None, None))
    if pool_fn is None or call_fn is None:
        return None, {"provider": provider, "error_type": "unknown_provider", "message": "unknown provider"}

    pool = pool_fn()
    if not pool:
        return None, None  # not configured: not attempted, not a failure

    last_error: dict[str, Any] | None = None
    for credential in pool:
        try:
            payload = call_fn(credential, query, max_results=max_results, timeout=timeout)
        except DataSourceError as exc:
            last_error = _sanitized_error(provider, exc)
            _record_health(provider, False)
            if exc.status_code in KEY_ROTATION_STATUS_CODES:
                continue
            # Non-rotation failure (5xx, network, timeout): this key attempt
            # failed for a reason another key in the same pool would not fix
            # differently in a way we can distinguish here, but trying the
            # remaining keys is still safer than giving up on the whole
            # provider from one transient error.
            continue
        else:
            _record_health(provider, True)
            return normalize_fn(payload, provider), None
    return None, last_error


def search(
    query: str,
    *,
    providers: list[str] | None = None,
    max_results: int | None = None,
    freshness_days: float | None = None,
    config_path: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Run a web search through the provider degrade chain.

    Returns ``{"status", "provider_used", "items", "errors"}``.
    ``status`` is one of ``ok`` (at least one non-empty result from some
    provider), ``empty`` (a provider answered successfully with zero
    results), ``all_failed`` (every configured provider errored), or
    ``disabled`` (no provider has any credential/URL configured).
    """
    config = _load_config(config_path)
    order = providers if providers else _provider_order(config)
    moment = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    if max_results is None:
        config_max = config.get("max_results")
        max_results = (
            config_max
            if isinstance(config_max, int) and not isinstance(config_max, bool) and config_max > 0
            else DEFAULT_MAX_RESULTS
        )
    max_results = max(1, int(max_results))

    errors: list[dict[str, Any]] = []
    attempted_any = False

    for provider in order:
        timeout = float(_provider_setting(config, provider, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        items, error = _run_provider(provider, query, max_results=max_results, timeout=timeout)
        if items is None and error is None:
            continue  # provider not configured
        attempted_any = True
        if error is not None:
            errors.append(error)
            continue
        marked = _mark_freshness(items or [], freshness_days=freshness_days, now=moment)
        if marked:
            return {
                "status": "ok",
                "provider_used": provider,
                "items": marked,
                "errors": errors,
            }
        return {
            "status": "empty",
            "provider_used": provider,
            "items": [],
            "errors": errors,
        }

    if not attempted_any:
        return {"status": "disabled", "provider_used": None, "items": [], "errors": []}

    return {"status": "all_failed", "provider_used": None, "items": [], "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多供应商网络搜索 CLI")
    parser.add_argument("query", help="搜索查询词")
    parser.add_argument("--max-results", type=int, default=None, help=f"默认取配置项 max_results 或 {DEFAULT_MAX_RESULTS}")
    parser.add_argument("--freshness-days", type=float, default=None)
    parser.add_argument("--providers", help="逗号分隔的 provider 顺序覆盖，如 tavily,bocha")
    parser.add_argument("--config", default=None, help="config/web_search.json 覆盖路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    providers = [p.strip() for p in args.providers.split(",") if p.strip()] if args.providers else None
    result = search(
        args.query,
        providers=providers,
        max_results=args.max_results,
        freshness_days=args.freshness_days,
        config_path=args.config,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"status={result['status']} provider_used={result['provider_used']}")
        for item in result["items"]:
            stale_flag = " [stale]" if item.get("stale") else ""
            print(f"- {item['title']}{stale_flag}\n  {item['url']}")
        if result["errors"]:
            for err in result["errors"]:
                print(f"! {err['provider']}: {err.get('message')}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual CLI entry
    raise SystemExit(main())
