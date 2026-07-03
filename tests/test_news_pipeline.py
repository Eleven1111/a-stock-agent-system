import pytest

import news_pipeline


L1_CONFIG = {
    "rank_weight": {"S5": 5, "S4": 4, "S3": 3, "S2": 2, "S1": 1, "S0": 0},
    "materiality_keywords": {
        "critical": ["降准", "印花税"],
        "high": ["专项债"],
        "medium": ["调研"],
    },
    "min_title_len": 4,
    "generic_titles": ["更多", "首页"],
    "pass_threshold_score": 5,
    "queue_max_entries": 50,
    "excerpt_max_chars": 60,
}


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    return tmp_path


def _item(title, rank="S5", url="https://example.gov.cn/a"):
    return {
        "title": title,
        "url": url,
        "source_id": "gov_test",
        "source_name": "测试官方源",
        "source_rank": rank,
    }


def test_score_item_passes_high_rank_with_critical_keyword():
    scored = news_pipeline.score_item(_item("央行宣布全面降准0.5个百分点"), L1_CONFIG)
    assert scored["passed"] is True
    assert scored["keyword_tier"] == "critical"
    assert scored["l1_score"] == 8
    assert "降准" in scored["matched_keywords"]


def test_score_item_requires_keyword_not_just_rank():
    scored = news_pipeline.score_item(_item("今日天气晴朗适合出行"), L1_CONFIG)
    assert scored["passed"] is False
    assert scored["matched_keywords"] == []


def test_score_item_low_rank_with_weak_keyword_fails_threshold():
    scored = news_pipeline.score_item(_item("某机构调研上市公司", rank="S1"), L1_CONFIG)
    assert scored["passed"] is False
    assert scored["l1_score"] == 2


def test_score_item_drops_noise_titles():
    assert news_pipeline.score_item(_item("更多"), L1_CONFIG) is None
    assert news_pipeline.score_item(_item("abc"), L1_CONFIG) is None


def test_dedupe_items_by_content_fingerprint():
    items = [_item("央行宣布全面降准0.5个百分点"), _item("央行宣布全面降准0.5个百分点")]
    fresh, dups = news_pipeline.dedupe_items(items)
    assert len(fresh) == 1
    assert dups == 1
    again, dups2 = news_pipeline.dedupe_items([_item("央行宣布全面降准0.5个百分点")])
    assert again == []
    assert dups2 == 1


def _enqueue_one(title="央行宣布全面降准0.5个百分点"):
    scored = news_pipeline.score_item(_item(title), L1_CONFIG)
    fresh, _ = news_pipeline.dedupe_items([scored])
    return news_pipeline.enqueue_l1_items(fresh, now="2026-07-03T09:00:00+08:00")


def test_enqueue_claim_and_submit_cycle():
    assert _enqueue_one() == 1
    assert _enqueue_one("财政部研究调整印花税政策") == 1

    batch = news_pipeline.claim_l1_batch(
        "openclaw", batch_size=10, now="2026-07-03T09:05:00+08:00",
    )
    assert len(batch) == 2
    assert all(entry["status"] == "claimed" for entry in batch)
    assert news_pipeline.claim_l1_batch("hermes", now="2026-07-03T09:06:00+08:00") == []

    result = news_pipeline.submit_l2_grades([
        {"fingerprint": batch[0]["fingerprint"], "materiality": 3,
         "affected_sectors": ["银行"], "time_window": "1-3d",
         "needs_deep_review": True},
        {"fingerprint": batch[1]["fingerprint"], "materiality": 1,
         "affected_sectors": [], "time_window": "unknown",
         "needs_deep_review": False},
    ])
    assert result["graded"] == 2
    assert result["missing"] == []
    summary = news_pipeline.queue_summary()
    assert summary["by_status"] == {"graded": 2}


def test_claim_ttl_recovers_lost_batch():
    _enqueue_one()
    first = news_pipeline.claim_l1_batch(
        "openclaw", ttl_minutes=30, now="2026-07-03T09:00:00+08:00",
    )
    assert len(first) == 1
    assert news_pipeline.claim_l1_batch(
        "hermes", ttl_minutes=30, now="2026-07-03T09:10:00+08:00",
    ) == []
    recovered = news_pipeline.claim_l1_batch(
        "hermes", ttl_minutes=30, now="2026-07-03T09:31:00+08:00",
    )
    assert len(recovered) == 1
    assert recovered[0]["claimed_by"] == "hermes"
    assert recovered[0]["attempts"] == 2


def test_claim_expires_after_max_attempts():
    _enqueue_one()
    news_pipeline.claim_l1_batch("a", ttl_minutes=1, max_attempts=2,
                                 now="2026-07-03T09:00:00+08:00")
    news_pipeline.claim_l1_batch("b", ttl_minutes=1, max_attempts=2,
                                 now="2026-07-03T09:02:00+08:00")
    third = news_pipeline.claim_l1_batch("c", ttl_minutes=1, max_attempts=2,
                                         now="2026-07-03T09:04:00+08:00")
    assert third == []
    assert news_pipeline.queue_summary()["by_status"] == {"expired": 1}


def test_submit_reports_unknown_fingerprints_as_missing():
    result = news_pipeline.submit_l2_grades([
        {"fingerprint": "nope", "materiality": 2},
    ])
    assert result["graded"] == 0
    assert result["missing"] == ["nope"]


def test_run_l1_scan_splits_passed_and_rejected():
    collected = [
        _item("央行宣布全面降准0.5个百分点"),
        _item("今日天气晴朗适合出行"),
        _item("更多"),
    ]
    result = news_pipeline.run_l1_scan(collected, L1_CONFIG)
    assert result["scanned"] == 3
    assert result["scored"] == 2
    assert len(result["passed"]) == 1
    assert result["rejected_count"] == 1
