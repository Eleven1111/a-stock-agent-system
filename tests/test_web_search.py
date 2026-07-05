import json
import subprocess
import sys

import pytest

import web_search
from http_client import DataSourceError


@pytest.fixture(autouse=True)
def _isolated_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))


def _fake_result(data, attempts=1):
    class _Result:
        pass

    result = _Result()
    result.data = data
    result.fetched_at = "2026-07-04T09:00:00+00:00"
    result.attempts = attempts
    return result


def _http_error(source, status_code):
    return DataSourceError(
        source,
        f"HTTP {status_code}: rejected",
        error_type="http",
        status_code=status_code,
    )


# ─── provider response normalization ───


def test_tavily_normalizes_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEYS", "tavily-key-1")
    payload = {
        "results": [
            {
                "title": "A股行情速览",
                "url": "https://example.com/a",
                "content": "市场概况……",
                "published_date": "2026-07-03",
            }
        ]
    }

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        assert source == "tavily"
        return _fake_result(payload)

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    result = web_search.search("A股 行情", providers=["tavily"])
    assert result["status"] == "ok"
    assert result["provider_used"] == "tavily"
    item = result["items"][0]
    assert item["title"] == "A股行情速览"
    assert item["url"] == "https://example.com/a"
    assert item["snippet"] == "市场概况……"
    assert item["published"] == "2026-07-03"
    assert item["provider"] == "tavily"


def test_bocha_normalizes_results(monkeypatch):
    monkeypatch.setenv("BOCHA_API_KEYS", "bocha-key-1")
    payload = {
        "data": {
            "webPages": {
                "value": [
                    {
                        "name": "博查搜索结果标题",
                        "url": "https://example.com/b",
                        "snippet": "摘要内容",
                        "datePublished": "2026-07-02",
                    }
                ]
            }
        }
    }

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        assert source == "bocha"
        return _fake_result(payload)

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    result = web_search.search("query", providers=["bocha"])
    assert result["status"] == "ok"
    assert result["provider_used"] == "bocha"
    item = result["items"][0]
    assert item["title"] == "博查搜索结果标题"
    assert item["url"] == "https://example.com/b"
    assert item["snippet"] == "摘要内容"
    assert item["published"] == "2026-07-02"
    assert item["provider"] == "bocha"


def test_searxng_normalizes_results(monkeypatch):
    monkeypatch.setenv("SEARXNG_BASE_URLS", "https://searx.example.com")
    payload = {
        "results": [
            {
                "title": "SearXNG结果",
                "url": "https://example.com/c",
                "content": "内容片段",
                "publishedDate": "2026-07-01",
            }
        ]
    }

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        assert source == "searxng"
        return _fake_result(payload)

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    result = web_search.search("query", providers=["searxng"])
    assert result["status"] == "ok"
    assert result["provider_used"] == "searxng"
    item = result["items"][0]
    assert item["title"] == "SearXNG结果"
    assert item["url"] == "https://example.com/c"
    assert item["snippet"] == "内容片段"
    assert item["published"] == "2026-07-01"
    assert item["provider"] == "searxng"


# ─── multi-key rotation ───


def test_429_triggers_key_rotation(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEYS", "key-bad,key-good")
    calls = []

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        body = json.loads(request.data.decode("utf-8"))
        calls.append(body["api_key"])
        if body["api_key"] == "key-bad":
            raise _http_error("tavily", 429)
        return _fake_result({"results": [{"title": "ok", "url": "https://x", "content": "c"}]})

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    result = web_search.search("query", providers=["tavily"])
    assert result["status"] == "ok"
    assert calls == ["key-bad", "key-good"]


@pytest.mark.parametrize("status_code", [429, 402, 401])
def test_key_marked_unusable_on_quota_and_auth_errors(monkeypatch, status_code):
    monkeypatch.setenv("TAVILY_API_KEYS", "key-bad,key-good")
    calls = []

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        body = json.loads(request.data.decode("utf-8"))
        calls.append(body["api_key"])
        if body["api_key"] == "key-bad":
            raise _http_error("tavily", status_code)
        return _fake_result({"results": []})

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    result = web_search.search("query", providers=["tavily"])
    assert calls == ["key-bad", "key-good"]
    assert result["status"] == "empty"


def test_all_keys_exhausted_fails_provider(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEYS", "key-1,key-2")

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        raise _http_error("tavily", 429)

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    result = web_search.search("query", providers=["tavily"])
    assert result["status"] == "all_failed"
    assert result["provider_used"] is None
    assert len(result["errors"]) == 1
    assert result["errors"][0]["provider"] == "tavily"


# ─── provider degrade chain ───


def test_degrade_chain_falls_through_to_next_provider(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEYS", "t-key")
    monkeypatch.setenv("BOCHA_API_KEYS", "b-key")

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        if source == "tavily":
            raise _http_error("tavily", 500)
        if source == "bocha":
            return _fake_result(
                {"data": {"webPages": {"value": [{"name": "t", "url": "https://x", "snippet": "s"}]}}}
            )
        raise AssertionError(f"unexpected source {source}")

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    result = web_search.search("query")
    assert result["status"] == "ok"
    assert result["provider_used"] == "bocha"
    assert len(result["errors"]) == 1
    assert result["errors"][0]["provider"] == "tavily"


def test_degrade_chain_default_order(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEYS", "t-key")
    monkeypatch.setenv("BOCHA_API_KEYS", "b-key")
    monkeypatch.setenv("SEARXNG_BASE_URLS", "https://searx.example.com")
    seen = []

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        seen.append(source)
        raise _http_error(source, 500)

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    result = web_search.search("query")
    assert result["status"] == "all_failed"
    assert seen == ["tavily", "bocha", "searxng"]


def test_degrade_chain_order_overridable_by_config(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEYS", "t-key")
    monkeypatch.setenv("BOCHA_API_KEYS", "b-key")
    config_file = tmp_path / "web_search.json"
    config_file.write_text(json.dumps({"provider_order": ["bocha", "tavily"]}), encoding="utf-8")
    seen = []

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        seen.append(source)
        raise _http_error(source, 500)

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    result = web_search.search("query", config_path=str(config_file))
    assert result["status"] == "all_failed"
    assert seen == ["bocha", "tavily"]


def test_config_overrides_max_results_and_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEYS", "t-key")
    config_file = tmp_path / "web_search.json"
    config_file.write_text(
        json.dumps({"max_results": 3, "providers": {"tavily": {"timeout_seconds": 5}}}),
        encoding="utf-8",
    )
    seen = {}

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        seen["timeout"] = timeout
        seen["max_results"] = json.loads(request.data.decode("utf-8"))["max_results"]
        return _fake_result({"results": []})

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    web_search.search("query", config_path=str(config_file))
    assert seen == {"timeout": 5.0, "max_results": 3}


# ─── fail-closed contracts ───


def test_no_providers_configured_is_disabled(monkeypatch):
    for var in (
        "TAVILY_API_KEYS", "TAVILY_API_KEY",
        "BOCHA_API_KEYS", "BOCHA_API_KEY",
        "SEARXNG_BASE_URLS", "SEARXNG_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)

    result = web_search.search("query")
    assert result["status"] == "disabled"
    assert result["items"] == []
    assert result["provider_used"] is None


def test_all_providers_fail_is_all_failed_never_fabricates_empty(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEYS", "t-key")

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        raise _http_error(source, 500)

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    result = web_search.search("query", providers=["tavily"])
    assert result["status"] == "all_failed"
    assert result["items"] == []
    assert result["errors"]


def test_empty_results_status_is_empty_not_ok(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEYS", "t-key")

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        return _fake_result({"results": []})

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    result = web_search.search("query", providers=["tavily"])
    assert result["status"] == "empty"
    assert result["items"] == []


def test_api_key_never_appears_in_error_messages(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEYS", "super-secret-key-value")

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        raise _http_error("tavily", 500)

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    result = web_search.search("query", providers=["tavily"])
    serialized = json.dumps(result, ensure_ascii=False)
    assert "super-secret-key-value" not in serialized


# ─── provider health recording ───


def test_provider_health_records_success_and_failure(monkeypatch):
    import provider_health

    monkeypatch.setenv("TAVILY_API_KEYS", "t-key")
    monkeypatch.setenv("BOCHA_API_KEYS", "b-key")

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        if source == "tavily":
            raise _http_error("tavily", 500)
        return _fake_result(
            {"data": {"webPages": {"value": [{"name": "t", "url": "https://x", "snippet": "s"}]}}}
        )

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    web_search.search("query")
    tavily_score = provider_health.health_score("tavily", "web_search")
    bocha_score = provider_health.health_score("bocha", "web_search")
    assert tavily_score["samples"] == 1 and tavily_score["successes"] == 0
    assert bocha_score["samples"] == 1 and bocha_score["successes"] == 1


# ─── freshness marking ───


def test_freshness_marks_stale_without_dropping(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEYS", "t-key")
    payload = {
        "results": [
            {"title": "fresh", "url": "https://x/1", "content": "c", "published_date": "2026-07-03"},
            {"title": "stale", "url": "https://x/2", "content": "c", "published_date": "2026-01-01"},
            {"title": "no-date", "url": "https://x/3", "content": "c"},
        ]
    }

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        return _fake_result(payload)

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    result = web_search.search(
        "query", providers=["tavily"], freshness_days=7, now="2026-07-04T12:00:00+00:00",
    )
    assert len(result["items"]) == 3
    by_title = {item["title"]: item for item in result["items"]}
    assert by_title["fresh"]["stale"] is False
    assert by_title["stale"]["stale"] is True
    assert by_title["no-date"]["stale"] is False


# ─── CLI ───


def test_cli_json_output(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEYS", "t-key")

    def fake_request_json(request, *, source, timeout, max_attempts, headers=None):
        return _fake_result({"results": [{"title": "t", "url": "https://x", "content": "c"}]})

    monkeypatch.setattr(web_search, "request_json", fake_request_json)

    exit_code = web_search.main(["some query", "--max-results", "3", "--json", "--providers", "tavily"])
    assert exit_code == 0


def test_cli_subprocess_disabled_status(tmp_path):
    env = {"PATH": "/usr/bin:/bin"}
    repo_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()
    proc = subprocess.run(
        [sys.executable, f"{repo_root}/skills/common/web_search.py", "test query", "--json"],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "disabled"
