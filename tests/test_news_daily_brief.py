"""Tests for the daily news processing brief."""

from __future__ import annotations

import json
import os

import pytest

from scripts import news_daily_brief


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    return tmp_path


def _write_l1_run(state_home, date_str, filename, data):
    """Write an L1 run artifact for a given date."""
    run_dir = state_home / "skills" / "news-pipeline" / "data" / "l1_runs" / date_str
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / filename).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _write_l1_queue(state_home, entries):
    """Write the L1 queue file."""
    queue_path = state_home / "skills" / "news-pipeline" / "data" / "l1_queue.json"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _write_l1_seen(state_home, fingerprints):
    """Write the L1 seen fingerprints file."""
    seen_path = state_home / "skills" / "news-pipeline" / "data" / "l1_seen.json"
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(json.dumps({
        "schema": "news_l1_seen_fingerprints_v1",
        "fingerprints": fingerprints,
    }), encoding="utf-8")


def _make_run(checked_at, collected=100, scored=80, passed=5, rejected=75,
              duplicates=10, enqueued=3, new_items=None, failed_sources=None):
    return {
        "schema": "news_l1_scan_v1",
        "checked_at": checked_at,
        "status": "ready" if enqueued > 0 else "no_signal",
        "has_signal": enqueued > 0,
        "summary": {
            "ok_sources": 12,
            "failed_sources": len(failed_sources or []),
            "collected_count": collected,
            "l1_scored": scored,
            "l1_passed": passed,
            "l1_rejected": rejected,
            "duplicate_count": duplicates,
            "enqueued_count": enqueued,
        },
        "queue": {"total": 50, "by_status": {"pending": 3, "graded": 47}},
        "failed_source_ids": failed_sources or [],
        "new_items": new_items or [],
    }


def _make_queue_entry(title, source_name, source_rank, collected_at, status="pending",
                      keywords=None, tier=None, url=None, fingerprint=None):
    return {
        "schema": "news_l1_entry_v1",
        "fingerprint": fingerprint or f"fp_{hash(title) % 10000:04d}",
        "title": title,
        "url": url or f"https://example.com/{hash(title) % 10000}",
        "source_id": f"src_{source_name}",
        "source_name": source_name,
        "source_rank": source_rank,
        "matched_keywords": keywords or [],
        "keyword_tier": tier,
        "l1_score": 5,
        "excerpt": title[:200],
        "published_hint": collected_at,
        "collected_at": collected_at,
        "status": status,
        "claimed_by": None,
        "claimed_at": None,
        "attempts": 0,
    }


# ── aggregate_runs ───────────────────────────────────────────────────────


def test_aggregate_runs_sums_all_stats():
    runs = [
        _make_run("2026-07-16T09:00:00+08:00", collected=100, passed=5, enqueued=3),
        _make_run("2026-07-16T11:00:00+08:00", collected=80, passed=3, enqueued=2),
    ]
    agg = news_daily_brief.aggregate_runs(runs)
    assert agg["run_count"] == 2
    assert agg["total_scanned"] == 180
    assert agg["total_l1_passed"] == 8
    assert agg["total_enqueued"] == 5


def test_aggregate_runs_handles_empty():
    agg = news_daily_brief.aggregate_runs([])
    assert agg["run_count"] == 0
    assert agg["total_scanned"] == 0


def test_aggregate_runs_collects_failed_sources():
    runs = [
        _make_run("2026-07-16T09:00:00+08:00", failed_sources=["src_a", "src_b"]),
        _make_run("2026-07-16T11:00:00+08:00", failed_sources=["src_b", "src_c"]),
    ]
    agg = news_daily_brief.aggregate_runs(runs)
    assert sorted(agg["failed_source_ids"]) == ["src_a", "src_b", "src_c"]


def test_aggregate_runs_merges_new_items():
    items1 = [{"title": "新闻1", "source_name": "新华社"}]
    items2 = [{"title": "新闻2", "source_name": "人民日报"}]
    runs = [
        _make_run("2026-07-16T09:00:00+08:00", new_items=items1),
        _make_run("2026-07-16T11:00:00+08:00", new_items=items2),
    ]
    agg = news_daily_brief.aggregate_runs(runs)
    assert len(agg["new_items"]) == 2


# ── classify helpers ─────────────────────────────────────────────────────


def test_classify_by_source():
    items = [
        {"source_name": "新华社"}, {"source_name": "新华社"},
        {"source_name": "人民日报"},
    ]
    result = news_daily_brief.classify_by_source(items)
    assert result == {"新华社": 2, "人民日报": 1}


def test_classify_by_keyword_tier():
    items = [
        {"keyword_tier": "critical"}, {"keyword_tier": "high"},
        {"keyword_tier": "critical"}, {"keyword_tier": None},
    ]
    result = news_daily_brief.classify_by_keyword_tier(items)
    assert result["critical"] == 2
    assert result["high"] == 1
    assert result["未分类"] == 1


def test_classify_by_keywords():
    items = [
        {"matched_keywords": ["降准", "央行"]},
        {"matched_keywords": ["降准", "政策"]},
    ]
    result = news_daily_brief.classify_by_keywords(items)
    assert result["降准"] == 2
    assert result["央行"] == 1
    assert result["政策"] == 1


def test_classify_queue_by_status():
    items = [
        {"status": "pending"}, {"status": "pending"},
        {"status": "graded"}, {"status": "expired"},
    ]
    result = news_daily_brief.classify_queue_by_status(items)
    assert result == {"pending": 2, "graded": 1, "expired": 1}


# ── format_markdown ──────────────────────────────────────────────────────


def test_format_markdown_contains_header_and_date():
    agg = news_daily_brief.aggregate_runs([])
    md = news_daily_brief.format_markdown("2026-07-16", agg, [], 0)
    assert "# 📰 新闻处理日报 | 2026-07-16" in md
    assert "管道概览" in md


def test_format_markdown_shows_pipeline_stats():
    runs = [_make_run("2026-07-16T09:00:00+08:00", collected=100, passed=5, enqueued=3)]
    agg = news_daily_brief.aggregate_runs(runs)
    md = news_daily_brief.format_markdown("2026-07-16", agg, [], 0)
    assert "采集总量**: 100" in md
    assert "L1 通过**: 5" in md
    assert "新入队列**: 3" in md


def test_format_markdown_shows_queue_status():
    queue = [
        _make_queue_entry("新闻A", "新华社", "S3", "2026-07-16T09:00:00+08:00", status="graded"),
        _make_queue_entry("新闻B", "新华社", "S3", "2026-07-16T09:00:00+08:00", status="pending"),
    ]
    agg = news_daily_brief.aggregate_runs([])
    md = news_daily_brief.format_markdown("2026-07-16", agg, queue, 0)
    assert "队列状态" in md
    assert "已分级" in md
    assert "待L2分级" in md


def test_format_markdown_shows_source_distribution():
    items = [
        _make_queue_entry("新闻A", "新华社", "S3", "2026-07-16T09:00:00+08:00"),
        _make_queue_entry("新闻B", "新华社", "S3", "2026-07-16T09:00:00+08:00"),
        _make_queue_entry("新闻C", "人民日报", "S3", "2026-07-16T09:00:00+08:00"),
    ]
    agg = news_daily_brief.aggregate_runs([])
    md = news_daily_brief.format_markdown("2026-07-16", agg, items, 0)
    assert "来源分布" in md
    assert "新华社" in md
    assert "人民日报" in md


def test_format_markdown_shows_news_items():
    items = [
        _make_queue_entry(
            "央行宣布全面降准0.5个百分点", "新华社", "S3",
            "2026-07-16T09:00:00+08:00",
            keywords=["降准", "央行"], tier="critical",
        ),
    ]
    agg = news_daily_brief.aggregate_runs([])
    md = news_daily_brief.format_markdown("2026-07-16", agg, items, 0)
    assert "央行宣布全面降准0.5个百分点" in md
    assert "新华社" in md
    assert "降准" in md


def test_format_markdown_shows_empty_state():
    agg = news_daily_brief.aggregate_runs([])
    md = news_daily_brief.format_markdown("2026-07-16", agg, [], 0)
    assert "当日无新闻进入处理管道" in md


def test_format_markdown_shows_failed_sources():
    runs = [_make_run("2026-07-16T09:00:00+08:00", failed_sources=["src_x"])]
    agg = news_daily_brief.aggregate_runs(runs)
    md = news_daily_brief.format_markdown("2026-07-16", agg, [], 0)
    assert "采集失败源" in md
    assert "src_x" in md


def test_format_markdown_respects_max_items():
    items = [
        _make_queue_entry(f"新闻{i}", "新华社", "S3", "2026-07-16T09:00:00+08:00")
        for i in range(10)
    ]
    agg = news_daily_brief.aggregate_runs([])
    md = news_daily_brief.format_markdown("2026-07-16", agg, items, 0, max_items=3)
    assert "前 3/10" in md


def test_format_markdown_contains_generation_timestamp():
    agg = news_daily_brief.aggregate_runs([])
    md = news_daily_brief.format_markdown("2026-07-16", agg, [], 0)
    assert "生成时间" in md


# ── build_daily_brief (integration) ─────────────────────────────────────


def test_build_daily_brief_reads_l1_runs(state_home):
    _write_l1_run(state_home, "2026-07-16", "run1.json", _make_run(
        "2026-07-16T09:00:00+08:00", collected=100, passed=5, enqueued=3,
        new_items=[{"title": "央行降准", "source_name": "新华社", "source_rank": "S3"}],
    ))
    _write_l1_seen(state_home, ["fp1", "fp2", "fp3"])

    result = news_daily_brief.build_daily_brief("2026-07-16")
    assert result["schema"] == "news_daily_brief_v1"
    assert result["date"] == "2026-07-16"
    assert result["aggregate"]["run_count"] == 1
    assert result["aggregate"]["total_scanned"] == 100
    assert result["seen_fingerprints_count"] == 3
    assert "央行降准" in result["markdown"]


def test_build_daily_brief_reads_l1_queue(state_home):
    queue = [
        _make_queue_entry("国务院发布新政策", "中国政府网", "S5", "2026-07-16T09:30:00+08:00",
                          status="graded", keywords=["政策", "国务院"], tier="high"),
        _make_queue_entry("旧新闻", "新华社", "S3", "2026-07-15T09:00:00+08:00"),
    ]
    _write_l1_queue(state_home, queue)

    result = news_daily_brief.build_daily_brief("2026-07-16")
    assert result["queue_items_count"] == 1  # Only 2026-07-16 items
    assert "国务院发布新政策" in result["markdown"]


def test_build_daily_brief_handles_no_data(state_home):
    result = news_daily_brief.build_daily_brief("2026-07-16")
    assert result["aggregate"]["run_count"] == 0
    assert result["queue_items_count"] == 0
    assert "当日无新闻进入处理管道" in result["markdown"]


def test_build_daily_brief_json_output(state_home):
    _write_l1_run(state_home, "2026-07-16", "run1.json", _make_run(
        "2026-07-16T09:00:00+08:00", collected=50, passed=2, enqueued=1,
    ))
    result = news_daily_brief.build_daily_brief("2026-07-16")
    # Should be valid JSON-serializable
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    parsed = json.loads(serialized)
    assert parsed["schema"] == "news_daily_brief_v1"


# ── Edge cases ───────────────────────────────────────────────────────────


def test_format_markdown_handles_items_with_missing_fields():
    items = [
        {"title": "完整条目", "source_name": "新华社", "source_rank": "S3",
         "collected_at": "2026-07-16T09:00:00+08:00", "matched_keywords": ["降准"],
         "keyword_tier": "critical", "url": "https://example.com/1"},
        {"title": None, "source_name": None},  # Minimal fields
    ]
    agg = news_daily_brief.aggregate_runs([])
    md = news_daily_brief.format_markdown("2026-07-16", agg, items, 0)
    assert "完整条目" in md
    assert "(无标题)" in md


def test_aggregate_runs_handles_malformed_summary():
    runs = [
        {"checked_at": "2026-07-16T09:00:00+08:00"},  # No summary
        {"summary": None},  # Null summary
    ]
    agg = news_daily_brief.aggregate_runs(runs)
    assert agg["run_count"] == 2
    assert agg["total_scanned"] == 0
