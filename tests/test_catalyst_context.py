"""Catalyst context cache feeds four_dim catalyst scoring."""

from datetime import datetime

import catalyst_context as ctx
import four_dim_scorer as fds


def test_catalyst_context_round_trip_by_stock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx.update_catalyst_context(
        [
            {"stock_code": "600001", "title": "公司中标大额订单", "date": "1 day ago"},
            {"stock_code": "000002", "title": "其他公司公告", "date": "1 day ago"},
        ],
        generated_at=datetime(2026, 6, 17, 10, 0),
    )

    events = ctx.read_catalyst_events(
        "600001",
        now=datetime(2026, 6, 17, 11, 0),
    )

    assert [event["title"] for event in events] == ["公司中标大额订单"]


def test_score_catalyst_uses_cached_context_when_serpapi_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx.update_catalyst_context(
        [{"stock_code": "600001", "title": "公司中标大额订单", "date": "1 day ago"}],
        generated_at=datetime.now(),
    )
    monkeypatch.setattr(fds, "fetch_serpapi_news", lambda *args, **kwargs: None)

    result = fds.score_catalyst("600001", "测试股")

    assert result["available"] is True
    assert result["source_status"] == "cache_only"
    assert result["score"] > 5.0


def test_catalyst_context_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx.update_catalyst_context(
        [{"stock_code": "600001", "title": "公司中标大额订单", "date": "1 day ago"}],
        generated_at=datetime(2026, 6, 15, 10, 0),
    )

    events = ctx.read_catalyst_events(
        "600001",
        now=datetime(2026, 6, 17, 15, 0),
        max_age_hours=24,
    )

    assert events == []


def test_events_without_stock_code_do_not_refresh_existing_stock_events(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx.update_catalyst_context(
        [{"stock_code": "600001", "title": "公司中标大额订单", "date": "1 day ago"}],
        generated_at=datetime(2026, 6, 15, 10, 0),
    )
    ctx.update_catalyst_context(
        [{"title": "宏观政策新闻", "date": "1 day ago"}],
        generated_at=datetime(2026, 6, 17, 10, 0),
    )

    events = ctx.read_catalyst_events(
        "600001",
        now=datetime(2026, 6, 17, 15, 0),
        max_age_hours=24,
    )

    assert events == []


def test_unrelated_stock_update_does_not_refresh_stale_stock_events(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx.update_catalyst_context(
        [{"stock_code": "600001", "title": "旧订单催化", "date": "1 day ago"}],
        generated_at=datetime(2026, 6, 15, 10, 0),
    )
    ctx.update_catalyst_context(
        [{"stock_code": "000002", "title": "新公告催化", "date": "1 day ago"}],
        generated_at=datetime(2026, 6, 17, 10, 0),
    )

    events = ctx.read_catalyst_events(
        "600001",
        now=datetime(2026, 6, 17, 15, 0),
        max_age_hours=24,
    )

    assert events == []
