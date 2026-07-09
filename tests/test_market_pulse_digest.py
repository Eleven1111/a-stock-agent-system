"""Market pulse cron stays script-only and bounded for OpenClaw cold starts."""

import importlib.util
from types import SimpleNamespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_market_pulse_digest_uses_one_query_and_caps_summary(monkeypatch):
    pulse = load_module("market_pulse_digest_test", "scripts/market_pulse_digest.py")
    calls = []
    monkeypatch.setattr(pulse, "_serper_key", lambda: "test-key")
    monkeypatch.setattr(
        pulse,
        "fetch_serper_news",
        lambda query, api_key, limit: calls.append((query, api_key, limit))
        or SimpleNamespace(data=[
            {"title": "A股午后AI算力板块异动拉升", "source": "测试源", "date": "5 minutes ago"},
            {"title": "半导体设备延续强势但成交分化", "source": "测试源", "date": "8 minutes ago"},
        ]),
    )

    result = pulse.run_pulse(profile="midday", max_chars=60)

    assert result["status"] == "ready"
    assert len(calls) == 1
    assert result["query"] == pulse.PROFILES["midday"]["query"]
    assert len(result["summary"]) <= 60
    assert result["events_count"] == 2


def test_market_pulse_digest_fails_closed_without_key(monkeypatch):
    pulse = load_module("market_pulse_digest_no_key_test", "scripts/market_pulse_digest.py")
    monkeypatch.setattr(pulse, "_serper_key", lambda: None)

    result = pulse.run_pulse(profile="close")

    assert result["status"] == "insufficient_data"
    assert result["signals"] == []
