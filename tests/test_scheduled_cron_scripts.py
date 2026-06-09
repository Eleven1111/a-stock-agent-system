"""Cron scripts that remove Gateway-side template injection."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_batch_four_dim_targets_parse_defaults_and_custom():
    batch = load_module("batch_four_dim_scorer_test", "skills/stock-triage/scripts/batch_four_dim_scorer.py")

    assert ("002156", "通富微电") in batch.parse_targets(None)
    assert batch.parse_targets("002156:通富微电,600011:华能国际") == [
        ("002156", "通富微电"),
        ("600011", "华能国际"),
    ]


def test_scheduled_news_monitor_fails_closed_without_serpapi(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_KEYS", raising=False)
    monitor = load_module("scheduled_news_monitor_test", "skills/news-to-sector/scripts/scheduled_monitor.py")

    result = monitor.run_monitor(["半导体 A股"], limit=1)

    assert result["status"] == "insufficient_data"
    assert result["signals"] == []
