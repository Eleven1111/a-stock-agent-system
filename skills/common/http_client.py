"""Reusable urllib transport with bounded retries and typed failures."""

from __future__ import annotations

import json
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Generic, Optional, TypeVar


T = TypeVar("T")

# Ensure NO_PROXY covers Eastmoney domains so urllib never routes them through
# a system proxy (e.g. Clash Verge).  Must run before any urllib.request call
# because urllib caches proxy settings at first use.
_DIRECT_MARKET_DATA_HOSTS = (
    "push2.eastmoney.com,push2his.eastmoney.com,"
    "datacenter-web.eastmoney.com,reportapi.eastmoney.com,mxapi.eastmoney.com,"
    ".gtimg.cn,.sinajs.cn,.10jqka.com.cn,.hexun.com"
)
_no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy", "")
if not _no_proxy:
    os.environ["NO_PROXY"] = _DIRECT_MARKET_DATA_HOSTS
    os.environ["no_proxy"] = _DIRECT_MARKET_DATA_HOSTS
elif "eastmoney.com" not in _no_proxy or "gtimg.cn" not in _no_proxy:
    os.environ["NO_PROXY"] = f"{_no_proxy},{_DIRECT_MARKET_DATA_HOSTS}"
    os.environ["no_proxy"] = os.environ["NO_PROXY"]


def _build_proxy_aware_opener() -> Any:
    """Return a callable opener that respects system proxies and NO_PROXY.

    ``OpenerDirector`` is not directly callable, so we return its ``open``
    method — the same signature as ``urllib.request.urlopen``.
    """
    director = urllib.request.build_opener()
    return director.open


# ── Process-level throttle ──────────────────────────────────────────────
# Ensures a minimum interval between consecutive requests to the same
# source within this process.  Prevents burst traffic that can trigger
# server-side rate limits (e.g. Eastmoney push2 429s).
# The half-open probe-limiting is handled by ``provider_health.allow_request``
# which grants exactly one probe slot per cooldown window; the throttle here
# complements that by spacing out requests even when the circuit is closed.
_THROTTLE_INTERVAL: float = 2.5  # seconds (bumped from 2.0 to reduce push2 WAF trigger)
_last_request_ts: Dict[str, float] = {}
_throttle_lock = threading.Lock()


def _throttle_wait(source: str) -> None:
    """Block until at least ``_THROTTLE_INTERVAL`` seconds have elapsed since
    the last request to *source* in this process.

    Eastmoney requests get extra jitter (0.5-1.5s random) on top of the base
    interval to reduce the chance of triggering WAF sliding-window rate limits.
    """
    with _throttle_lock:
        last = _last_request_ts.get(source, 0.0)
        elapsed = time.monotonic() - last
        # Extra jitter for eastmoney sources to avoid WAF pattern detection
        extra_jitter = 0.0
        if "eastmoney" in source.lower():
            extra_jitter = random.uniform(0.5, 1.5)
        total_wait = _THROTTLE_INTERVAL + extra_jitter
        if elapsed < total_wait:
            time.sleep(total_wait - elapsed)
        _last_request_ts[source] = time.monotonic()


__all__ = [
    "DataSourceError",
    "ErrorType",
    "HttpClient",
    "HttpResult",
    "build_request",
    "request_bytes",
    "request_text",
    "request_json",
]


class ErrorType:
    UNKNOWN = "unknown"
    TIMEOUT = "timeout"
    HTTP = "http"
    NETWORK = "network"
    DECODE = "decode"
    INVALID_RESPONSE = "invalid_response"


class DataSourceError(Exception):
    """Stable data-source failure shared by generic and provider-specific clients."""

    def __init__(
        self,
        source: str,
        message: str,
        original: Optional[Exception] = None,
        *,
        error_type: str = ErrorType.UNKNOWN,
        attempts: int = 1,
        timestamp: Optional[str] = None,
        status_code: Optional[int] = None,
        retry_after_seconds: Optional[float] = None,
    ):
        self.source = source
        self.message = message
        self.original = original
        self.error_type = error_type
        self.attempts = attempts
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"[{source}:{error_type}] {message}")

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "source": self.source,
            "error_type": self.error_type,
            "error": self.message,
            "attempts": self.attempts,
            "timestamp": self.timestamp,
        }
        if self.status_code is not None:
            result["status_code"] = self.status_code
        if self.retry_after_seconds is not None:
            result["retry_after_seconds"] = self.retry_after_seconds
        return result


@dataclass(frozen=True)
class HttpResult(Generic[T]):
    data: T
    fetched_at: str
    attempts: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _request(value: str | urllib.request.Request, headers: Optional[Dict[str, str]]) -> urllib.request.Request:
    if isinstance(value, urllib.request.Request):
        return value
    return urllib.request.Request(value, headers=headers or {})


def build_request(
    url: str,
    *,
    data: bytes | None = None,
    headers: Optional[Dict[str, str]] = None,
    method: str | None = None,
) -> urllib.request.Request:
    """Construct a request without exposing urllib in business modules."""
    return urllib.request.Request(url, data=data, headers=headers or {}, method=method)


class HttpClient:
    """Generic bytes/text/json HTTP client. Total attempts are capped at two."""

    def __init__(
        self,
        source: str,
        *,
        timeout: float = 10,
        max_attempts: int = 2,
        opener: Optional[Callable[..., Any]] = None,
        clock: Callable[[], datetime] = _utc_now,
        sleeper: Callable[[float], None] = time.sleep,
        max_retry_after_seconds: float = 60,
    ):
        self.source = source
        self.timeout = float(timeout)
        self.max_attempts = min(max(int(max_attempts), 1), 2)
        self._opener = opener or _build_proxy_aware_opener()
        self._clock = clock
        self._sleeper = sleeper
        self._max_retry_after_seconds = max(0.0, float(max_retry_after_seconds))

    def _timestamp(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _record_health(self, ok: bool, latency_ms: float) -> None:
        """Best-effort SLO recording. Never allowed to affect request outcome.

        Lazily imported so http_client.py stays dependency-free at module
        load time (it is the lowest-level transport shared by every
        provider adapter); a missing/broken provider_health module must
        never break an HTTP call. Skipped inside
        ``provider_health.suppress_transport_recording()`` so a request
        already accounted at a higher layer (field_arbiter) is not
        double-counted in the transport "default" bucket.
        """
        try:
            from provider_health import record_result, transport_recording_suppressed
            if transport_recording_suppressed():
                return
            record_result(self.source, "default", ok, latency_ms)
        except Exception:  # noqa: BLE001 - health bookkeeping is non-critical
            pass

    def request_bytes(
        self,
        request: str | urllib.request.Request,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult[bytes]:
        _throttle_wait(self.source)
        request_obj = _request(request, headers)
        last_error: Optional[DataSourceError] = None
        started = time.monotonic()
        for attempt in range(1, self.max_attempts + 1):
            try:
                with self._opener(request_obj, timeout=self.timeout) as response:
                    payload = response.read()
                self._record_health(True, (time.monotonic() - started) * 1000)
                return HttpResult(payload, self._timestamp(), attempt)
            except (TimeoutError, socket.timeout) as exc:
                last_error = DataSourceError(
                    self.source,
                    str(exc) or "request timed out",
                    exc,
                    error_type=ErrorType.TIMEOUT,
                    attempts=attempt,
                    timestamp=self._timestamp(),
                )
            except urllib.error.HTTPError as exc:
                retry_after = None
                try:
                    raw_retry_after = exc.headers.get("Retry-After")
                    if raw_retry_after is not None:
                        try:
                            retry_after = max(0.0, float(raw_retry_after))
                        except ValueError:
                            retry_at = parsedate_to_datetime(raw_retry_after)
                            if retry_at.tzinfo is None:
                                retry_at = retry_at.replace(tzinfo=timezone.utc)
                            retry_after = max(
                                0.0,
                                (retry_at - self._clock()).total_seconds(),
                            )
                except (AttributeError, TypeError, ValueError):
                    retry_after = None
                last_error = DataSourceError(
                    self.source,
                    f"HTTP {exc.code}: {exc.reason}",
                    exc,
                    error_type=ErrorType.HTTP,
                    attempts=attempt,
                    timestamp=self._timestamp(),
                    status_code=exc.code,
                    retry_after_seconds=retry_after,
                )
                if exc.code != 429 and exc.code < 500:
                    break
                if retry_after is not None and attempt < self.max_attempts:
                    self._sleeper(min(retry_after, self._max_retry_after_seconds))
            except urllib.error.URLError as exc:
                error_type = (
                    ErrorType.TIMEOUT
                    if isinstance(exc.reason, (TimeoutError, socket.timeout))
                    else ErrorType.NETWORK
                )
                last_error = DataSourceError(
                    self.source,
                    str(exc.reason),
                    exc,
                    error_type=error_type,
                    attempts=attempt,
                    timestamp=self._timestamp(),
                )
            except OSError as exc:
                last_error = DataSourceError(
                    self.source,
                    str(exc),
                    exc,
                    error_type=ErrorType.NETWORK,
                    attempts=attempt,
                    timestamp=self._timestamp(),
                )
        self._record_health(False, (time.monotonic() - started) * 1000)
        if last_error is None:
            raise RuntimeError("HTTP request failed without an error")
        raise last_error

    def request_text(
        self,
        request: str | urllib.request.Request,
        *,
        encoding: str = "utf-8",
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult[str]:
        result = self.request_bytes(request, headers=headers)
        try:
            text = result.data.decode(encoding)
        except UnicodeDecodeError as exc:
            raise DataSourceError(
                self.source,
                f"{encoding} decode failed: {exc}",
                exc,
                error_type=ErrorType.DECODE,
                attempts=result.attempts,
                timestamp=result.fetched_at,
            ) from exc
        return HttpResult(text, result.fetched_at, result.attempts)

    def request_json(
        self,
        request: str | urllib.request.Request,
        *,
        encoding: str = "utf-8",
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult[Any]:
        result = self.request_text(request, encoding=encoding, headers=headers)
        try:
            payload = json.loads(result.data)
        except json.JSONDecodeError as exc:
            raise DataSourceError(
                self.source,
                f"JSON decode failed: {exc}",
                exc,
                error_type=ErrorType.INVALID_RESPONSE,
                attempts=result.attempts,
                timestamp=result.fetched_at,
            ) from exc
        return HttpResult(payload, result.fetched_at, result.attempts)


def request_bytes(
    request: str | urllib.request.Request,
    *,
    source: str,
    timeout: float = 10,
    max_attempts: int = 2,
    headers: Optional[Dict[str, str]] = None,
) -> HttpResult[bytes]:
    return HttpClient(source, timeout=timeout, max_attempts=max_attempts).request_bytes(
        request,
        headers=headers,
    )


def request_text(
    request: str | urllib.request.Request,
    *,
    source: str,
    timeout: float = 10,
    max_attempts: int = 2,
    encoding: str = "utf-8",
    headers: Optional[Dict[str, str]] = None,
) -> HttpResult[str]:
    return HttpClient(source, timeout=timeout, max_attempts=max_attempts).request_text(
        request,
        encoding=encoding,
        headers=headers,
    )


def request_json(
    request: str | urllib.request.Request,
    *,
    source: str,
    timeout: float = 10,
    max_attempts: int = 2,
    encoding: str = "utf-8",
    headers: Optional[Dict[str, str]] = None,
) -> HttpResult[Any]:
    return HttpClient(source, timeout=timeout, max_attempts=max_attempts).request_json(
        request,
        encoding=encoding,
        headers=headers,
    )
