"""Reusable urllib transport with bounded retries and typed failures."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Generic, Optional, TypeVar


T = TypeVar("T")

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
    ):
        self.source = source
        self.timeout = float(timeout)
        self.max_attempts = min(max(int(max_attempts), 1), 2)
        self._opener = opener or urllib.request.urlopen
        self._clock = clock

    def _timestamp(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def request_bytes(
        self,
        request: str | urllib.request.Request,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult[bytes]:
        request_obj = _request(request, headers)
        last_error: Optional[DataSourceError] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with self._opener(request_obj, timeout=self.timeout) as response:
                    payload = response.read()
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
