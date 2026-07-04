"""Natural-language screening recall channel: parsing, fail-closed config gates,
recall_source tagging, and channel-status orchestration."""

from __future__ import annotations

import json

import pytest

import nl_screening as nls
from http_client import DataSourceError, ErrorType, HttpResult


@pytest.fixture(autouse=True)
def isolated_provider_state(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("EASTMONEY_QGQP_B_ID", raising=False)
    monkeypatch.delenv("WENCAI_API_KEY", raising=False)


# ─── Eastmoney: normal parsing ───


def test_eastmoney_search_parses_object_rows(monkeypatch):
    monkeypatch.setenv("EASTMONEY_QGQP_B_ID", "fake-fingerprint")
    captured = {}

    def fake_request_json(request, **kwargs):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["kwargs"] = kwargs
        return HttpResult(
            {
                "result": {
                    "columns": [{"key": "code"}, {"key": "name"}],
                    "dataList": [
                        {"code": "002156", "name": "通富微电"},
                        {"code": "600000", "name": "浦发银行"},
                    ],
                }
            },
            "2026-07-04T01:00:00+00:00",
            1,
        )

    monkeypatch.setattr(nls, "request_json", fake_request_json)

    rows = nls.eastmoney_search("换手率大于3%小于25%")

    assert rows == [
        {
            "code": "002156",
            "name": "通富微电",
            "recall_source": "nl_screening_eastmoney",
            "recall_query": "换手率大于3%小于25%",
        },
        {
            "code": "600000",
            "name": "浦发银行",
            "recall_source": "nl_screening_eastmoney",
            "recall_query": "换手率大于3%小于25%",
        },
    ]
    assert captured["body"]["fingerprint"] == "fake-fingerprint"
    assert captured["body"]["keyWord"] == "换手率大于3%小于25%"
    assert "Origin" in dict((k.title(), v) for k, v in captured["headers"].items())
    header_titles = {k.title() for k in captured["headers"]}
    assert {"Origin", "Referer", "Content-Type"} <= header_titles


def test_eastmoney_search_parses_array_rows_with_columns(monkeypatch):
    monkeypatch.setenv("EASTMONEY_QGQP_B_ID", "fake-fingerprint")

    def fake_request_json(request, **kwargs):
        return HttpResult(
            {
                "result": {
                    "columns": [{"key": "dm"}, {"key": "mc"}],
                    "dataList": [["300750", "宁德时代"]],
                }
            },
            "2026-07-04T01:00:00+00:00",
            1,
        )

    monkeypatch.setattr(nls, "request_json", fake_request_json)

    rows = nls.eastmoney_search("10日内有过涨停")
    assert rows == [{
        "code": "300750",
        "name": "宁德时代",
        "recall_source": "nl_screening_eastmoney",
        "recall_query": "10日内有过涨停",
    }]


# ─── Eastmoney: not configured (fail-closed disable) ───


def test_eastmoney_search_without_fingerprint_raises_data_source_error():
    with pytest.raises(DataSourceError) as caught:
        nls.eastmoney_search("非ST")
    assert "EASTMONEY_QGQP_B_ID" in caught.value.message


def test_recall_candidates_reports_eastmoney_disabled_without_fingerprint():
    report = nls.recall_candidates(queries=["非ST"])
    eastmoney_channels = [c for c in report["channels"] if c["source"] == "nl_screening_eastmoney"]
    assert len(eastmoney_channels) == 1
    assert eastmoney_channels[0]["status"] == "disabled"
    assert report["candidates"] == []


# ─── Eastmoney: interface failure is fail-closed (blocked, not empty) ───


def test_eastmoney_search_http_failure_raises_not_empty(monkeypatch):
    monkeypatch.setenv("EASTMONEY_QGQP_B_ID", "fake-fingerprint")

    def fake_request_json(request, **kwargs):
        raise DataSourceError(
            "nl_screening_eastmoney", "HTTP 502: Bad Gateway",
            error_type=ErrorType.HTTP, status_code=502,
        )

    monkeypatch.setattr(nls, "request_json", fake_request_json)

    with pytest.raises(DataSourceError):
        nls.eastmoney_search("非ST")


def test_eastmoney_search_malformed_response_raises_invalid_response(monkeypatch):
    monkeypatch.setenv("EASTMONEY_QGQP_B_ID", "fake-fingerprint")

    def fake_request_json(request, **kwargs):
        return HttpResult({"unexpected": "shape"}, "2026-07-04T01:00:00+00:00", 1)

    monkeypatch.setattr(nls, "request_json", fake_request_json)

    with pytest.raises(DataSourceError) as caught:
        nls.eastmoney_search("非ST")
    assert caught.value.error_type == ErrorType.INVALID_RESPONSE


def test_recall_candidates_marks_eastmoney_blocked_on_failure(monkeypatch):
    monkeypatch.setenv("EASTMONEY_QGQP_B_ID", "fake-fingerprint")

    def failing_fetcher(query, **kwargs):
        raise DataSourceError("nl_screening_eastmoney", "boom", error_type=ErrorType.NETWORK)

    report = nls.recall_candidates(queries=["非ST"], eastmoney_fetcher=failing_fetcher)
    eastmoney_channels = [c for c in report["channels"] if c["source"] == "nl_screening_eastmoney"]
    assert eastmoney_channels[0]["status"] == "blocked"
    assert eastmoney_channels[0]["error"]
    # A blocked channel must never be conflated with a legitimate empty match.
    assert report["candidates"] == []


# ─── Wencai: optional enhancement, gated on API key ───


def test_wencai_search_without_api_key_raises_data_source_error():
    with pytest.raises(DataSourceError) as caught:
        nls.wencai_search("流通市值小于100亿")
    assert "WENCAI_API_KEY" in caught.value.message


def test_recall_candidates_reports_wencai_disabled_without_key():
    report = nls.recall_candidates(queries=["非ST"])
    wencai_channels = [c for c in report["channels"] if c["source"] == "nl_screening_wencai"]
    assert wencai_channels[0]["status"] == "disabled"


def test_wencai_search_parses_response_when_key_present(monkeypatch):
    monkeypatch.setenv("WENCAI_API_KEY", "fake-key")
    captured = {}

    def fake_request_json(request, **kwargs):
        captured["headers"] = dict(request.header_items())
        return HttpResult(
            {"data": {"data": [{"code": "002415", "name": "海康威视"}]}},
            "2026-07-04T01:00:00+00:00",
            1,
        )

    monkeypatch.setattr(nls, "request_json", fake_request_json)

    rows = nls.wencai_search("量比大于1")
    assert rows == [{
        "code": "002415",
        "name": "海康威视",
        "recall_source": "nl_screening_wencai",
        "recall_query": "量比大于1",
    }]
    header_titles = {k.title() for k in captured["headers"]}
    assert "Authorization" in header_titles


def test_recall_candidates_tags_wencai_source_when_enabled(monkeypatch):
    monkeypatch.setenv("WENCAI_API_KEY", "fake-key")

    def fake_wencai(query, **kwargs):
        return [{"code": "002415", "name": "海康威视", "recall_source": "nl_screening_wencai", "recall_query": query}]

    def disabled_eastmoney(query, **kwargs):
        raise DataSourceError("nl_screening_eastmoney", "unused")

    report = nls.recall_candidates(queries=["非ST"], wencai_fetcher=fake_wencai)
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["recall_source"] == "nl_screening_wencai"


# ─── Orchestration: multiple queries + dedupe across channels ───


def test_recall_candidates_dedupes_same_code_across_queries(monkeypatch):
    monkeypatch.setenv("EASTMONEY_QGQP_B_ID", "fp")

    def fetcher(query, **kwargs):
        return [{"code": "002156", "name": "通富微电", "recall_source": "nl_screening_eastmoney", "recall_query": query}]

    report = nls.recall_candidates(
        queries=["非ST", "流通市值小于100亿"],
        eastmoney_fetcher=fetcher,
    )
    assert report["candidate_count"] == 1
    assert len(report["candidates"]) == 1


def test_recall_candidates_uses_config_queries_by_default(monkeypatch):
    monkeypatch.setenv("EASTMONEY_QGQP_B_ID", "fp")
    seen_queries = []

    def fetcher(query, **kwargs):
        seen_queries.append(query)
        return []

    nls.recall_candidates(eastmoney_fetcher=fetcher)
    config_queries = nls.load_config()["queries"]
    assert seen_queries == config_queries
    # Guard against accidental hardcoded stock/sector names in the template.
    joined = " ".join(config_queries)
    for forbidden in ("宁德时代", "贵州茅台", "半导体板块"):
        assert forbidden not in joined
