import importlib.util
import os
import subprocess
import sys
import types

from http_client import HttpResult


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(ROOT, "skills", "global-market-monitor", "scripts", "monitor.py")


def load_monitor_module(name: str = "global_monitor_yfinance_test"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_yfinance_rate_limit_trips_process_local_breaker(monkeypatch):
    monitor = load_monitor_module("global_monitor_yfinance_breaker_test")

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        @property
        def info(self):
            raise RuntimeError("429 Client Error: Too Many Requests")

    fake_yfinance = types.SimpleNamespace(Ticker=FakeTicker)
    monkeypatch.setitem(sys.modules, "yfinance", fake_yfinance)

    first = monitor.fetch_yfinance_batch(["^GSPC", "^IXIC"])
    assert first["_error"].startswith("yfinance rate limited")
    assert first["^IXIC"]["error"].startswith("yfinance rate limited")

    second = monitor.fetch_yfinance_batch(["^DJI"])
    assert second["_error"].startswith("yfinance rate limited")
    assert second["^DJI"]["error"].startswith("yfinance rate limited")


def test_yfinance_batch_timeout_trips_process_local_breaker(monkeypatch):
    monitor = load_monitor_module("global_monitor_yfinance_timeout_test")
    calls = []

    fake_yfinance = types.SimpleNamespace(download=lambda **kwargs: None)
    monkeypatch.setitem(sys.modules, "yfinance", fake_yfinance)

    def timeout_worker(*args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(monitor.subprocess, "run", timeout_worker)

    first = monitor.fetch_yfinance_batch(["^GSPC"])
    assert first["_error"] == "yfinance batch timed out after 12s"
    assert len(calls) == 1

    second = monitor.fetch_yfinance_batch(["^IXIC"])
    assert second["_error"] == "yfinance batch timed out after 12s"
    assert len(calls) == 1


def test_collect_all_data_marks_insufficient_when_yfinance_is_disabled(monkeypatch):
    monitor = load_monitor_module("global_monitor_source_health_test")
    monitor._YFINANCE_DISABLED_REASON = "yfinance rate limited: test"
    monitor.USE_SINA = False

    monkeypatch.setattr(monitor, "fetch_natural_disasters", lambda: [])
    monkeypatch.setattr(monitor, "fetch_serper_news", lambda *args, **kwargs: [])
    monkeypatch.setattr(monitor, "fetch_geopolitical_news", lambda: [])

    data = monitor.collect_all_data()

    assert data["source_health"]["yfinance"]["status"] == "failed"
    assert "rate limited" in data["source_health"]["yfinance"]["error"]
    assert data["impact"]["status"] == "insufficient_data"


def test_http_sources_use_provider_clients_and_preserve_decoding(monkeypatch):
    monitor = load_monitor_module("global_monitor_http_provider_test")
    calls = []

    class FakeClient:
        def __init__(self, source):
            self.source = source

        def request_text(self, request, **kwargs):
            calls.append((self.source, "text", request, kwargs))
            if self.source == "sina":
                return HttpResult(
                    'var hq_str_gb_$dji="道琼斯,42000,1.2,500"',
                    "2026-06-12T06:00:00+00:00",
                    1,
                )
            return HttpResult("<rss><title>flood warning</title></rss>", "2026-06-12T06:00:00+00:00", 1)

        def request_json(self, request, **kwargs):
            calls.append((self.source, "json", request, kwargs))
            if self.source == "serper":
                return HttpResult(
                    {"news_results": [{"title": "Fed update", "source": {"name": "Wire"}}]},
                    "2026-06-12T06:00:00+00:00",
                    1,
                )
            return HttpResult({"features": []}, "2026-06-12T06:00:00+00:00", 1)

    monkeypatch.setattr(monitor, "provider_client", lambda source: FakeClient(source))
    monkeypatch.setattr(monitor, "_next_serper_key", lambda: "secret")
    monkeypatch.setattr(
        monitor,
        "_fetch_serper_news",
        lambda query, api_key, num: HttpResult(
            [{"title": "Fed update", "source": "Wire"}],
            "2026-06-12T06:00:00+00:00",
            1,
        ),
    )

    assert monitor.fetch_sina_us_indices()["^DJI"]["price"] == 42000.0
    assert monitor.fetch_serper_news(num=1)[0]["title"] == "Fed update"
    assert monitor.fetch_natural_disasters()[0]["type"] == "洪水"

    assert [call[0] for call in calls] == ["sina", "usgs", "gdacs"]
    assert calls[0][3] == {
        "encoding": "gbk",
        "headers": {
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0",
        },
    }
    assert calls[1][3] == {"headers": {"User-Agent": "GlobalMarketMonitor/1.0"}}
    assert calls[2][3] == {
        "encoding": "utf-8",
        "headers": {"User-Agent": "GlobalMarketMonitor/1.0"},
    }


def test_global_monitor_uses_central_market_configuration():
    monitor = load_monitor_module("global_monitor_central_config_test")

    assert monitor.USE_YFINANCE is True
    assert monitor.THRESHOLDS["key_stock_move_notable"] == 5.0
    assert monitor.US_INDICES["^GSPC"]["name"] == "标普500"
    assert monitor.A_SHARE_SECTOR_STOCK_MAP["AI算力"][0] == ["000977", "浪潮信息"]


def test_sina_indices_keep_analysis_available_when_yfinance_fails(monkeypatch):
    monitor = load_monitor_module("global_monitor_sina_fallback_test")
    monitor._YFINANCE_DISABLED_REASON = "yfinance rate limited: test"
    monkeypatch.setattr(
        monitor,
        "fetch_sina_us_indices",
        lambda: {
            "^GSPC": {"price": 6000.0, "change_pct": 0.5},
            "^IXIC": {"price": 19000.0, "change_pct": 0.7},
            "^DJI": {"price": 42000.0, "change_pct": 0.2},
        },
    )
    monkeypatch.setattr(monitor, "fetch_natural_disasters", lambda: [])
    monkeypatch.setattr(monitor, "fetch_serper_news", lambda *args, **kwargs: [])
    monkeypatch.setattr(monitor, "fetch_geopolitical_news", lambda: [])

    data = monitor.collect_all_data()

    assert data["source_health"]["yfinance"]["status"] == "failed"
    assert data["source_health"]["sina"]["status"] == "ok"
    assert data["impact"].get("status") != "insufficient_data"
