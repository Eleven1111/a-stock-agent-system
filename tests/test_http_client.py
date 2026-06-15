"""Generic HTTP transport contract: bytes/text/json, retry, errors, timestamp."""

from datetime import datetime, timezone
from pathlib import Path
import urllib.error

import pytest

import http_client
from a_stock_http import DataSourceError as LegacyDataSourceError
from http_client import DataSourceError, ErrorType, HttpClient


FIXED_TIME = datetime(2026, 6, 12, 5, 30, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.payload


def test_module_level_bytes_text_json_apis(monkeypatch):
    payloads = iter([
        FakeResponse(b"raw"),
        FakeResponse("中文".encode()),
        FakeResponse(b'{"ok": true}'),
    ])
    monkeypatch.setattr(http_client.urllib.request, "urlopen", lambda request, timeout: next(payloads))

    assert http_client.request_bytes("https://example.test/raw", source="test").data == b"raw"
    assert http_client.request_text("https://example.test/text", source="test").data == "中文"
    assert http_client.request_json("https://example.test/json", source="test").data == {"ok": True}


def test_timeout_retries_once_and_preserves_timeout_and_timestamp():
    calls = []

    def opener(request, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise TimeoutError("slow")
        return FakeResponse(b"ok")

    client = HttpClient(
        "test",
        timeout=7,
        max_attempts=2,
        opener=opener,
        clock=lambda: FIXED_TIME,
    )

    result = client.request_text("https://example.test", encoding="utf-8")

    assert result.data == "ok"
    assert result.attempts == 2
    assert result.fetched_at == "2026-06-12T05:30:00+00:00"
    assert calls == [7.0, 7.0]


def test_timeout_failure_is_typed_and_never_exceeds_two_attempts():
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        raise TimeoutError("slow")

    client = HttpClient(
        "test",
        timeout=1,
        max_attempts=99,
        opener=opener,
        clock=lambda: FIXED_TIME,
    )

    with pytest.raises(DataSourceError) as caught:
        client.request_bytes("https://example.test")

    assert calls == 2
    assert caught.value.error_type == ErrorType.TIMEOUT
    assert caught.value.attempts == 2
    assert caught.value.timestamp == "2026-06-12T05:30:00+00:00"


def test_invalid_json_uses_invalid_response_error():
    client = HttpClient(
        "test",
        opener=lambda request, timeout: FakeResponse(b"not-json"),
        clock=lambda: FIXED_TIME,
        timeout=1,
        max_attempts=1,
    )

    with pytest.raises(DataSourceError) as caught:
        client.request_json("https://example.test")

    assert caught.value.error_type == ErrorType.INVALID_RESPONSE
    assert caught.value.attempts == 1


def test_http_error_exposes_retry_after_header():
    error = urllib.error.HTTPError(
        "https://example.test",
        429,
        "Too Many Requests",
        {"Retry-After": "3"},
        None,
    )
    client = HttpClient(
        "test",
        opener=lambda request, timeout: (_ for _ in ()).throw(error),
        clock=lambda: FIXED_TIME,
        timeout=1,
        max_attempts=1,
    )

    with pytest.raises(DataSourceError) as caught:
        client.request_json("https://example.test")

    assert caught.value.retry_after_seconds == 3.0
    assert caught.value.to_dict()["retry_after_seconds"] == 3.0


def test_a_stock_http_reexports_the_same_error_type():
    assert LegacyDataSourceError is DataSourceError


def test_business_modules_do_not_bypass_shared_http_transport():
    root = Path(__file__).resolve().parents[1]
    violations = []
    for base in (root / "skills", root / "scripts"):
        for path in base.rglob("*.py"):
            if path == root / "skills" / "common" / "http_client.py":
                continue
            source = path.read_text(encoding="utf-8")
            if "urlopen(" in source or "urllib.request" in source or "from urllib.request" in source:
                violations.append(str(path.relative_to(root)))
    assert violations == []
