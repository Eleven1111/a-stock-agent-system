"""Cron scripts that remove Gateway-side template injection."""

import importlib.util
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_batch_four_dim_targets_parse_pool_and_custom(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    batch = load_module("batch_four_dim_scorer_test", "skills/stock-triage/scripts/batch_four_dim_scorer.py")
    pool_path = tmp_path / "skills" / "stock-triage" / "data" / "candidate_pool_latest.json"
    pool_path.parent.mkdir(parents=True)
    pool_path.write_text(
        f'{{"status":"ready","asof":"{date.today().isoformat()}","candidates":['
        '{"code":"002156","name":"通富微电"},'
        '{"code":"600011","name":"华能国际"}]}',
        encoding="utf-8",
    )

    assert batch.parse_targets(None, limit=1) == [("002156", "通富微电")]
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


def test_scheduled_news_monitor_adds_active_registry_queries(monkeypatch):
    monitor = load_module("scheduled_news_monitor_registry_test", "skills/news-to-sector/scripts/scheduled_monitor.py")
    monkeypatch.setattr(
        monitor,
        "active_entries",
        lambda kind=None: [
            {"kind": "theme", "key": "AI算力", "label": "AI算力"},
            {"kind": "stock", "key": "002156", "label": "通富微电"},
        ],
    )

    queries = monitor.build_queries()

    assert any("AI算力" in query for query in queries)
    assert any("通富微电" in query and "澄清" in query for query in queries)


def test_scheduled_news_monitor_marks_clarification_as_risk():
    monitor = load_module("scheduled_news_monitor_risk_test", "skills/news-to-sector/scripts/scheduled_monitor.py")

    event = monitor.classify_event({
        "title": "公司澄清AI订单传闻",
        "snippet": "相关消息不属实，尚未形成收入",
    })

    assert event["risk_classification"]["is_risk"] is True
    assert "澄清" in event["risk_classification"]["clarification_hits"]
