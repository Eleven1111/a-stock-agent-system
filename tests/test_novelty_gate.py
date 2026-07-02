import json
from datetime import datetime, timedelta, timezone

from skills.common import novelty_gate


def _policy(mode="enforce"):
    return {
        "novelty_gate": {
            "enabled": True,
            "mode": mode,
            "ttl_days": 7,
        }
    }


def test_new_item_passes_and_is_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    now = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)

    result = novelty_gate.filter_items(
        [{"title": "  国务院发布 AI 产业政策！", "url": "https://example.test/a"}],
        namespace="news-monitor",
        job_id="news-monitor",
        now=now,
        policy=_policy(),
    )

    assert result.items[0]["title"].strip().startswith("国务院")
    assert result.duplicate_count == 0
    cache = json.loads(novelty_gate.cache_path().read_text(encoding="utf-8"))
    assert cache["entries"]


def test_repeated_item_is_suppressed_by_normalized_key(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    now = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    first = {"title": "国 务 院发布AI产业政策", "url": "https://a.example/1"}
    duplicate = {"title": "国务院发布 AI 产业政策！", "url": "https://b.example/2"}

    novelty_gate.filter_items(
        [first],
        namespace="news-monitor",
        job_id="news-monitor",
        now=now,
        policy=_policy(),
    )
    result = novelty_gate.filter_items(
        [duplicate],
        namespace="news-monitor-intraday",
        job_id="news-monitor-intraday",
        now=now + timedelta(minutes=1),
        policy=_policy(),
    )

    assert result.items == []
    assert result.duplicate_count == 1
    assert novelty_gate.duplicate_archive_note(result) == "另有 1 条重复资讯已归档"


def test_expired_cache_allows_item_again(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    old = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    fresh = old + timedelta(days=8)
    item = {"title": "证监会发布并购重组政策", "url": "https://example.test/policy"}

    novelty_gate.filter_items(
        [item],
        namespace="official-policy-watch",
        job_id="official-policy-watch",
        now=old,
        policy=_policy(),
    )
    result = novelty_gate.filter_items(
        [item],
        namespace="official-policy-watch",
        job_id="official-policy-watch",
        now=fresh,
        policy=_policy(),
    )

    assert result.items == [item]
    assert result.duplicate_count == 0


def test_corrupt_cache_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    path = novelty_gate.cache_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")

    item = {"title": "发改委发布机器人产业政策", "url": "https://example.test/r"}
    result = novelty_gate.filter_items(
        [item],
        namespace="official-policy-watch",
        job_id="official-policy-watch",
        now=datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
        policy=_policy(),
    )

    assert result.items == [item]
    assert result.fail_open is True
    assert result.duplicate_count == 0


def test_shadow_mode_keeps_items_and_records_would_suppress(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    now = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    item = {"title": "央行降准释放长期资金", "url": "https://example.test/rrr"}

    novelty_gate.filter_items(
        [item],
        namespace="news-monitor",
        job_id="news-monitor",
        now=now,
        policy=_policy("shadow"),
    )
    result = novelty_gate.filter_items(
        [item],
        namespace="news-monitor",
        job_id="news-monitor",
        now=now + timedelta(minutes=5),
        policy=_policy("shadow"),
    )

    assert result.items == [item]
    assert result.duplicate_count == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "cron" / "push_telemetry.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[-1]["would_suppress"] is True
    assert rows[-1]["suppression_reason"] == "duplicate_news"
