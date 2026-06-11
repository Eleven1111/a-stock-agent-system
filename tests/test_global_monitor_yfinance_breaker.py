import importlib.util
import os
import subprocess
import sys
import types


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

    monkeypatch.setattr(monitor, "fetch_natural_disasters", lambda: [])
    monkeypatch.setattr(monitor, "fetch_serpapi_news", lambda *args, **kwargs: [])
    monkeypatch.setattr(monitor, "fetch_geopolitical_news", lambda: [])

    data = monitor.collect_all_data()

    assert data["source_health"]["yfinance"]["status"] == "failed"
    assert "rate limited" in data["source_health"]["yfinance"]["error"]
    assert data["impact"]["status"] == "insufficient_data"
