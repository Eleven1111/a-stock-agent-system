"""Natural-language stock screening: second recall channel for candidate discovery.

The primary candidate-discovery channel (`candidate_discovery.py`) enumerates the
full SSE/SZSE universe. This module adds a second, independent recall channel
that queries a natural-language stock-screener backend with a generic condition
template (never a hardcoded stock or sector name) and returns matched codes
tagged with a `recall_source` so they can be merged into the same universe and
flow through the existing candidate FSM and ranking untouched.

Two backends:

- Eastmoney smart-tag search (`np-tjxg-g.eastmoney.com`), the free primary
  channel. Requires a `fingerprint` value (the `qgqp_b_id` browser cookie) from
  `EASTMONEY_QGQP_B_ID`; without it the channel is disabled and reported as such
  (fail-closed — never silently skipped as if it were healthy-but-empty).
- iwencai (同花顺问财) OpenAPI, an optional enhancement gated on `WENCAI_API_KEY`.

Both backends are read-only recall sources: a failure here must never be
interpreted as "no candidates" — the caller receives an explicit
blocked/disabled channel status distinct from a legitimate empty match list.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Mapping, Sequence

from config_registry import load_registered
from http_client import DataSourceError, ErrorType, build_request, request_json

try:
    import provider_health
except ImportError:  # pragma: no cover - script-style sys.path imports
    provider_health = None  # type: ignore[assignment]


EASTMONEY_PROVIDER = "nl_screening_eastmoney"
WENCAI_PROVIDER = "nl_screening_wencai"
MIAOXIANG_PROVIDER = "nl_screening_miaoxiang"
ENDPOINT_CLASS = "search"

RECALL_SOURCE_EASTMONEY = "nl_screening_eastmoney"
RECALL_SOURCE_WENCAI = "nl_screening_wencai"
RECALL_SOURCE_MIAOXIANG = "nl_screening_miaoxiang"

STATUS_OK = "ok"
STATUS_DISABLED = "disabled"
STATUS_BLOCKED = "blocked"

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def load_config() -> dict[str, Any]:
    return load_registered("nl_screening")


def _record_health(provider: str, ok: bool, latency_ms: float) -> None:
    if provider_health is None:
        return
    try:
        provider_health.record_result(provider, ENDPOINT_CLASS, ok, latency_ms)
    except Exception:  # noqa: BLE001 - health bookkeeping must never break recall
        pass


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text[2:] if text.startswith(("sh", "sz")) else text.zfill(6)


def _channel_result(
    *,
    status: str,
    source: str,
    query: str,
    candidates: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "source": source,
        "query": query,
        "candidate_count": len(candidates or []),
        "candidates": candidates or [],
        "error": error,
    }


# ─── Eastmoney smart-tag search ───


def eastmoney_fingerprint() -> str:
    return (os.environ.get("EASTMONEY_QGQP_B_ID") or "").strip()


def _eastmoney_headers() -> dict[str, str]:
    config = load_config()["eastmoney"]
    return {
        "Origin": str(config["origin"]),
        "Referer": str(config["referer"]),
        "Content-Type": "application/json",
        "User-Agent": _DEFAULT_USER_AGENT,
    }


def _eastmoney_body(query: str, fingerprint: str, page_size: int) -> dict[str, Any]:
    return {
        "keyWord": query,
        "pageSize": page_size,
        "pageNo": 1,
        "fingerprint": fingerprint,
        "gids": [],
        "matchWord": "",
        "timestamp": str(int(time.time())),
        "shareToGuba": False,
        "requestId": "",
        "needCorrect": True,
        "removedConditionIdList": [],
        "xcId": "",
        "ownSelectAll": False,
        "dxInfo": [],
        "extraCondition": "",
    }


def _parse_eastmoney_response(payload: Any, query: str) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise DataSourceError(
            EASTMONEY_PROVIDER,
            "response root is not an object",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise DataSourceError(
            EASTMONEY_PROVIDER,
            "response missing result object",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    columns = result.get("columns")
    data_list = result.get("dataList")
    if not isinstance(columns, list) or not isinstance(data_list, list):
        raise DataSourceError(
            EASTMONEY_PROVIDER,
            "result missing columns/dataList",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    column_keys = [str(col.get("key") or col.get("dataKey") or col.get("name") or "") for col in columns
                   if isinstance(col, Mapping)]
    candidates: list[dict[str, Any]] = []
    for row in data_list:
        if isinstance(row, Mapping):
            code = row.get("code") or row.get("SECURITY_CODE") or row.get("dm")
            name = row.get("name") or row.get("SECURITY_NAME_ABBR") or row.get("mc")
        elif isinstance(row, list) and column_keys:
            fields = dict(zip(column_keys, row))
            code = fields.get("code") or fields.get("dm") or fields.get("SECURITY_CODE")
            name = fields.get("name") or fields.get("mc") or fields.get("SECURITY_NAME_ABBR")
        else:
            continue
        if not code:
            continue
        candidates.append({
            "code": _code(code),
            "name": str(name or code),
            "recall_source": RECALL_SOURCE_EASTMONEY,
            "recall_query": query,
        })
    return candidates


def eastmoney_search(query: str, *, page_size: int | None = None) -> list[dict[str, Any]]:
    """Query the Eastmoney smart-tag natural-language screener.

    Raises DataSourceError (not configured / http / decode / invalid_response)
    on any failure. Never returns an empty list to mean "disabled" — that is
    a distinct, explicit status handled by `recall_candidates`.
    """
    fingerprint = eastmoney_fingerprint()
    if not fingerprint:
        raise DataSourceError(
            EASTMONEY_PROVIDER,
            "EASTMONEY_QGQP_B_ID not configured",
            error_type=ErrorType.UNKNOWN,
            attempts=0,
            timestamp="",
        )
    config = load_config()["eastmoney"]
    body = _eastmoney_body(query, fingerprint, int(page_size or config["page_size"]))
    request = build_request(
        str(config["base_url"]),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=_eastmoney_headers(),
        method="POST",
    )
    started = time.monotonic()
    try:
        result = request_json(
            request,
            source=EASTMONEY_PROVIDER,
            timeout=float(config["timeout_seconds"]),
            max_attempts=int(config["max_attempts"]),
        )
    except DataSourceError:
        _record_health(EASTMONEY_PROVIDER, False, (time.monotonic() - started) * 1000)
        raise
    candidates = _parse_eastmoney_response(result.data, query)
    _record_health(EASTMONEY_PROVIDER, True, (time.monotonic() - started) * 1000)
    return candidates


# ─── iwencai (同花顺问财) OpenAPI ───


def wencai_api_key() -> str:
    return (os.environ.get("WENCAI_API_KEY") or "").strip()


def _parse_wencai_response(payload: Any, query: str) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise DataSourceError(
            WENCAI_PROVIDER,
            "response root is not an object",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    data = payload.get("data")
    rows: Any = None
    if isinstance(data, Mapping):
        for key in ("data", "answer", "datas", "rows"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
    elif isinstance(data, list):
        rows = data
    if rows is None:
        raise DataSourceError(
            WENCAI_PROVIDER,
            "response missing recognizable data rows",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        code = row.get("code") or row.get("stock_code") or row.get("股票代码")
        name = row.get("name") or row.get("stock_name") or row.get("股票简称")
        if not code:
            continue
        candidates.append({
            "code": _code(code),
            "name": str(name or code),
            "recall_source": RECALL_SOURCE_WENCAI,
            "recall_query": query,
        })
    return candidates


def wencai_search(query: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Query the iwencai OpenAPI. Raises DataSourceError when not configured
    or on any transport/parse failure."""
    api_key = wencai_api_key()
    if not api_key:
        raise DataSourceError(
            WENCAI_PROVIDER,
            "WENCAI_API_KEY not configured",
            error_type=ErrorType.UNKNOWN,
            attempts=0,
            timestamp="",
        )
    config = load_config()["wencai"]
    body = {
        "query": query,
        "page": "1",
        "limit": str(int(limit or config["limit"])),
        "is_cache": "1",
    }
    request = build_request(
        str(config["base_url"]),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        result = request_json(
            request,
            source=WENCAI_PROVIDER,
            timeout=float(config["timeout_seconds"]),
            max_attempts=int(config["max_attempts"]),
        )
    except DataSourceError:
        _record_health(WENCAI_PROVIDER, False, (time.monotonic() - started) * 1000)
        raise
    candidates = _parse_wencai_response(result.data, query)
    _record_health(WENCAI_PROVIDER, True, (time.monotonic() - started) * 1000)
    return candidates


# ─── 妙想 MCP (东方财富智能数据服务) ───


def miaoxiang_api_key() -> str:
    return (os.environ.get("MIAOXIANG_API_KEY") or "").strip()


def _parse_miaoxiang_response(payload: Any, query: str) -> list[dict[str, Any]]:
    """Parse MCP JSON-RPC response for mx_stocks_screener tool."""
    if not isinstance(payload, Mapping):
        raise DataSourceError(
            MIAOXIANG_PROVIDER,
            "response root is not an object",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise DataSourceError(
            MIAOXIANG_PROVIDER,
            "response missing result object",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    content_list = result.get("content")
    if not isinstance(content_list, list) or not content_list:
        raise DataSourceError(
            MIAOXIANG_PROVIDER,
            "result missing content array",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    # MCP returns [{"type": "text", "text": "{...json...}"}]
    text_block = content_list[0]
    if not isinstance(text_block, Mapping) or text_block.get("type") != "text":
        raise DataSourceError(
            MIAOXIANG_PROVIDER,
            "content[0] is not a text block",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    try:
        inner = json.loads(text_block["text"])
    except (json.JSONDecodeError, KeyError) as exc:
        raise DataSourceError(
            MIAOXIANG_PROVIDER,
            f"failed to parse inner JSON: {exc}",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    data = inner.get("data")
    # data can be a list of sheet objects [{columns, items, sheetName}] or a dict
    if isinstance(data, list) and data:
        sheet = data[0]
    elif isinstance(data, Mapping):
        sheet = data
    else:
        raise DataSourceError(
            MIAOXIANG_PROVIDER,
            "inner JSON missing data object",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    if not isinstance(sheet, Mapping):
        raise DataSourceError(
            MIAOXIANG_PROVIDER,
            "data sheet is not an object",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    columns = sheet.get("columns") or []
    items = sheet.get("items") or []
    # Find the code and name column indices
    code_idx = -1
    name_idx = -1
    for i, col in enumerate(columns):
        col_str = str(col)
        if "代码" in col_str:
            code_idx = i
        elif "名称" in col_str:
            name_idx = i
    if code_idx < 0:
        raise DataSourceError(
            MIAOXIANG_PROVIDER,
            "cannot find code column in response",
            error_type=ErrorType.INVALID_RESPONSE,
        )
    candidates: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, list) or len(row) <= code_idx:
            continue
        code = _code(row[code_idx])
        name = str(row[name_idx]) if name_idx >= 0 and len(row) > name_idx else code
        if not code:
            continue
        candidates.append({
            "code": code,
            "name": name,
            "recall_source": RECALL_SOURCE_MIAOXIANG,
            "recall_query": query,
        })
    return candidates


def miaoxiang_search(query: str) -> list[dict[str, Any]]:
    """Query the 妙想 MCP stock screener via direct HTTP.

    Raises DataSourceError when not configured or on any failure.
    """
    api_key = miaoxiang_api_key()
    if not api_key:
        raise DataSourceError(
            MIAOXIANG_PROVIDER,
            "MIAOXIANG_API_KEY not configured",
            error_type=ErrorType.UNKNOWN,
            attempts=0,
            timestamp="",
        )
    config = load_config()["miaoxiang"]
    # MCP JSON-RPC 2.0 request
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": str(config.get("tool_name") or "mx_stocks_screener"),
            "arguments": {"query": query},
        },
    }
    request = build_request(
        str(config["base_url"]),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "em_api_key": api_key,
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        result = request_json(
            request,
            source=MIAOXIANG_PROVIDER,
            timeout=float(config["timeout_seconds"]),
            max_attempts=int(config["max_attempts"]),
        )
    except DataSourceError:
        _record_health(MIAOXIANG_PROVIDER, False, (time.monotonic() - started) * 1000)
        raise
    candidates = _parse_miaoxiang_response(result.data, query)
    _record_health(MIAOXIANG_PROVIDER, True, (time.monotonic() - started) * 1000)
    return candidates


# ─── Orchestration ───


def _run_channel(
    *,
    source: str,
    template_queries: Sequence[str],
    enabled: bool,
    disabled_reason: str | None,
    fetcher,
    all_candidates: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run one backend over every query template, appending its matches into
    the shared dedup map. A disabled channel (config-off or missing
    credential) produces one explicit "disabled" channel entry per query
    instead of silently contributing nothing indistinguishable from an
    empty-but-healthy result."""
    channels: list[dict[str, Any]] = []
    if not enabled:
        for query in template_queries:
            channels.append(_channel_result(
                status=STATUS_DISABLED, source=source, query=query, error=disabled_reason,
            ))
        return channels
    for query in template_queries:
        try:
            found = fetcher(query)
            channels.append(_channel_result(status=STATUS_OK, source=source, query=query, candidates=found))
            for item in found:
                all_candidates.setdefault(item["code"], item)
        except DataSourceError as exc:
            channels.append(_channel_result(status=STATUS_BLOCKED, source=source, query=query, error=str(exc)))
    return channels


def recall_candidates(
    *,
    queries: Sequence[str] | None = None,
    eastmoney_fetcher=eastmoney_search,
    wencai_fetcher=wencai_search,
    miaoxiang_fetcher=miaoxiang_search,
) -> dict[str, Any]:
    """Run every configured NL-screening backend over the generic query
    templates and return a per-channel status plus the union of candidates.

    Each channel's status is one of:
      - "ok": query succeeded (possibly with zero legitimate matches)
      - "disabled": prerequisite (API key/fingerprint) not configured
      - "blocked": query attempted and failed (network/parse/http)

    A "blocked" channel never contributes candidates and is never conflated
    with "ok" + empty results; callers must treat blocked as missing evidence,
    not as a negative signal.
    """
    config = load_config()
    template_queries = list(queries) if queries is not None else list(config.get("queries") or [])
    all_candidates: dict[str, dict[str, Any]] = {}

    eastmoney_cfg = config.get("eastmoney") or {}
    eastmoney_enabled = bool(eastmoney_cfg.get("enabled", True))
    eastmoney_disabled_reason = (
        "channel disabled in config" if not eastmoney_enabled
        else None if eastmoney_fingerprint()
        else "EASTMONEY_QGQP_B_ID not configured"
    )
    channels = _run_channel(
        source=RECALL_SOURCE_EASTMONEY,
        template_queries=template_queries,
        enabled=eastmoney_enabled and bool(eastmoney_fingerprint()),
        disabled_reason=eastmoney_disabled_reason,
        fetcher=eastmoney_fetcher,
        all_candidates=all_candidates,
    )

    wencai_cfg = config.get("wencai") or {}
    wencai_enabled = bool(wencai_cfg.get("enabled", True))
    wencai_disabled_reason = (
        "channel disabled in config" if not wencai_enabled
        else None if wencai_api_key()
        else "WENCAI_API_KEY not configured"
    )
    channels += _run_channel(
        source=RECALL_SOURCE_WENCAI,
        template_queries=template_queries,
        enabled=wencai_enabled and bool(wencai_api_key()),
        disabled_reason=wencai_disabled_reason,
        fetcher=wencai_fetcher,
        all_candidates=all_candidates,
    )

    miaoxiang_cfg = config.get("miaoxiang") or {}
    miaoxiang_enabled = bool(miaoxiang_cfg.get("enabled", True))
    miaoxiang_disabled_reason = (
        "channel disabled in config" if not miaoxiang_enabled
        else None if miaoxiang_api_key()
        else "MIAOXIANG_API_KEY not configured"
    )
    channels += _run_channel(
        source=RECALL_SOURCE_MIAOXIANG,
        template_queries=template_queries,
        enabled=miaoxiang_enabled and bool(miaoxiang_api_key()),
        disabled_reason=miaoxiang_disabled_reason,
        fetcher=miaoxiang_fetcher,
        all_candidates=all_candidates,
    )

    return {
        "schema": "nl_screening_recall_v1",
        "channels": channels,
        "candidate_count": len(all_candidates),
        "candidates": list(all_candidates.values()),
    }
