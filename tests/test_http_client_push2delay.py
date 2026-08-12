"""Tests for the Eastmoney push2 -> push2delay mirror fallback."""

import urllib.error
from urllib.parse import urlparse

import pytest

from http_client import (
    DataSourceError,
    ErrorType,
    HttpClient,
    _eastmoney_delay_mirror_request,
)


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_mirror_rewrites_push2_hosts():
    req = _eastmoney_delay_mirror_request(
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=3"
    )
    assert req is not None
    assert urlparse(req.full_url).netloc == "push2delay.eastmoney.com"

    req = _eastmoney_delay_mirror_request(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519"
    )
    assert req is not None
    assert urlparse(req.full_url).netloc == "push2delay.eastmoney.com"

    req = _eastmoney_delay_mirror_request(
        "https://17.push2.eastmoney.com/api/qt/clist/get"
    )
    assert req is not None
    assert urlparse(req.full_url).netloc == "push2delay.eastmoney.com"

    req = _eastmoney_delay_mirror_request(
        "https://82.push2.eastmoney.com/api/qt/clist/get"
    )
    assert req is not None
    assert urlparse(req.full_url).netloc == "push2delay.eastmoney.com"


def test_mirror_leaves_non_push2_hosts_untouched():
    for url in [
        "https://qt.gtimg.cn/q=sh600519",
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        "https://www.eastmoney.com/",
    ]:
        assert _eastmoney_delay_mirror_request(url) is None


def test_mirror_preserves_query_and_headers():
    req = _eastmoney_delay_mirror_request(
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=3&fid=f3",
        headers={"User-Agent": "test-agent", "Referer": "https://quote.eastmoney.com/"},
    )
    assert req is not None
    assert "pn=1" in req.full_url and "pz=3" in req.full_url
    assert req.headers.get("User-agent") == "test-agent"
    assert req.headers.get("Referer") == "https://quote.eastmoney.com/"


def test_transport_failure_retries_against_mirror(monkeypatch):
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise TimeoutError("primary CDN down")
        return FakeResponse(b"mirror-ok")

    client = HttpClient(
        "test",
        timeout=2,
        max_attempts=1,
        opener=opener,
    )
    result = client.request_bytes(
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1"
    )
    assert result.data == b"mirror-ok"
    assert result.attempts == 2  # 1 primary + 1 mirror
    assert len(calls) == 2
    assert urlparse(calls[1]).netloc == "push2delay.eastmoney.com"


def test_http_502_retries_against_mirror(monkeypatch):
    calls = []

    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__(
                "https://push2his.eastmoney.com",
                502,
                "Bad Gateway",
                hdrs={},
                fp=None,
            )

    def opener(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise FakeHTTPError()
        return FakeResponse(b"ok-after-502")

    client = HttpClient(
        "test",
        timeout=2,
        max_attempts=1,
        opener=opener,
    )
    result = client.request_bytes(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    )
    assert result.data == b"ok-after-502"
    assert urlparse(calls[1]).netloc == "push2delay.eastmoney.com"


def test_non_push2_failure_does_not_retry_mirror(monkeypatch):
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        raise TimeoutError("slow")

    client = HttpClient(
        "test",
        timeout=1,
        max_attempts=2,
        opener=opener,
    )
    with pytest.raises(DataSourceError) as caught:
        client.request_bytes("https://qt.gtimg.cn/q=sh600519")

    assert caught.value.error_type == ErrorType.TIMEOUT
    assert caught.value.attempts == 2
    assert len(calls) == 2  # no mirror attempt for non-push2 hosts


def test_mirror_failure_raises_combined_error(monkeypatch):
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        raise TimeoutError("everything down")

    client = HttpClient(
        "test",
        timeout=1,
        max_attempts=1,
        opener=opener,
    )
    with pytest.raises(DataSourceError) as caught:
        client.request_bytes("https://push2.eastmoney.com/api/qt/clist/get")

    assert "push2delay mirror also failed" in caught.value.message
    assert caught.value.attempts == 2
    assert len(calls) == 2
