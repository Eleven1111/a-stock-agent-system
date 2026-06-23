"""Tencent and Serper provider adapters share the generic HTTP client."""

from datetime import datetime, timezone

import data_provider
from a_stock_http import _TENCENT_FIELDS
from data_provider import fetch_serper_news, fetch_tencent_quote, fetch_tencent_quotes
from http_client import HttpClient, HttpResult


FIXED_TIME = datetime(2026, 6, 12, 5, 45, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.payload


def _tencent_line() -> bytes:
    parts = [""] * 50
    parts[_TENCENT_FIELDS["name"]] = "通富微电"
    parts[_TENCENT_FIELDS["price"]] = "23.45"
    parts[_TENCENT_FIELDS["prev_close"]] = "23.00"
    parts[_TENCENT_FIELDS["change_pct"]] = "1.96"
    parts[_TENCENT_FIELDS["high"]] = "23.80"
    parts[_TENCENT_FIELDS["low"]] = "23.00"
    parts[_TENCENT_FIELDS["volume"]] = "123456"
    parts[_TENCENT_FIELDS["amount"]] = "5000"
    parts[_TENCENT_FIELDS["turnover"]] = "4.20"
    return ('v_sz002156="' + "~".join(parts) + '"').encode("gbk")


def test_tencent_quote_has_provider_timestamp():
    client = HttpClient(
        "tencent",
        timeout=3,
        max_attempts=2,
        opener=lambda request, timeout: FakeResponse(_tencent_line()),
        clock=lambda: FIXED_TIME,
    )

    quote = fetch_tencent_quote("002156", client=client)

    assert quote["price"] == 23.45
    assert quote["amount"] == 50_000_000
    assert quote["provider"] == "tencent"
    assert quote["fetched_at"] == "2026-06-12T05:45:00+00:00"


def test_data_provider_delegates_tencent_transport_to_canonical_adapter(monkeypatch):
    expected = HttpResult(
        {"sz002156": {"price": 23.45}},
        "2026-06-12T05:45:00+00:00",
        1,
    )
    monkeypatch.setattr(
        data_provider,
        "_fetch_tencent_quotes_result",
        lambda codes, client=None: expected,
    )

    assert fetch_tencent_quotes(["002156"]) is expected


def test_serper_news_has_provider_timestamp_and_limit():
    payload = (
        '{"news":['
        '{"title":"新闻一","snippet":"摘要一","source":"来源一","link":"https://a","date":"1h ago"},'
        '{"title":"新闻二","snippet":"摘要二","source":"来源二","link":"https://b","date":"2h ago"}'
        "]}"
    ).encode()
    client = HttpClient(
        "serper",
        timeout=3,
        max_attempts=2,
        opener=lambda request, timeout: FakeResponse(payload),
        clock=lambda: FIXED_TIME,
    )

    result = fetch_serper_news("半导体 A股", "secret", 1, client=client)

    assert len(result.data) == 1
    assert result.data[0]["source"] == "来源一"
    assert result.data[0]["provider"] == "serper"
    assert result.data[0]["fetched_at"] == "2026-06-12T05:45:00+00:00"
