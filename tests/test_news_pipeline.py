import json
from pathlib import Path

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


def _item(title, rank="S5", url="https://example.gov.cn/a", summary=""):
    return {
        "title": title,
        "summary": summary,
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


def test_score_item_matches_keywords_in_preserved_summary():
    scored = news_pipeline.score_item(
        _item(
            "两大央企宣布增持A股股票资产",
            rank="S2",
            summary="中国国新使用回购增持专项再贷款及配套资金超500亿元。",
        ),
        {
            **L1_CONFIG,
            "materiality_keywords": {"critical": ["增持", "再贷款"]},
            "pass_threshold_score": 4,
        },
    )
    assert scored["passed"] is True
    assert set(scored["matched_keywords"]) == {"增持", "再贷款"}
    assert scored["excerpt"] == "中国国新使用回购增持专项再贷款及配套资金超500亿元。"
    assert scored["detail_status"] == "summary"


def test_repo_l1_accepts_state_capital_increase_with_preserved_summary():
    config_path = Path(__file__).resolve().parents[1] / "config" / "news_pipeline.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))

    scored = news_pipeline.score_item(
        _item(
            "两大央企宣布增持A股股票资产",
            rank="S2",
            summary="中国国新拟使用回购增持专项再贷款继续增持中央企业股票。",
        ),
        config["l1"],
    )

    assert scored["passed"] is True
    assert {"增持", "再贷款"} <= set(scored["matched_keywords"])


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


def test_enqueue_preserves_summary_and_detail_status_for_l2():
    scored = news_pipeline.score_item(
        _item(
            "两大央企宣布增持A股股票资产",
            summary="中国国新超500亿元，中国诚通近百亿元。",
        ),
        {**L1_CONFIG, "materiality_keywords": {"critical": ["增持"]}},
    )
    fresh, _ = news_pipeline.dedupe_items([scored])
    news_pipeline.enqueue_l1_items(fresh, now="2026-07-19T20:00:00+08:00")
    claimed = news_pipeline.claim_l1_batch("openclaw", now="2026-07-19T20:01:00+08:00")
    assert claimed[0]["summary"] == "中国国新超500亿元，中国诚通近百亿元。"
    assert claimed[0]["detail_status"] == "summary"


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


# ---------------------------------------------------------------------------
# read_graded_news: L2-graded news queried by code / sector for evidence packs
# ---------------------------------------------------------------------------

def _grade_batch(entries, grades_by_fp, *, now):
    """Claim then submit grades for a set of already-enqueued entries."""
    claimed = news_pipeline.claim_l1_batch(
        "openclaw", batch_size=len(grades_by_fp), now=now,
    )
    grades = []
    for entry in claimed:
        fp = entry["fingerprint"]
        grade = dict(grades_by_fp[entry["title"]])
        grade["fingerprint"] = fp
        grades.append(grade)
    return news_pipeline.submit_l2_grades(grades, now=now)


def test_submit_l2_grades_persists_affected_codes_field():
    _enqueue_one("央行宣布全面降准0.5个百分点")
    batch = news_pipeline.claim_l1_batch(
        "openclaw", now="2026-07-03T09:05:00+08:00",
    )
    news_pipeline.submit_l2_grades([
        {"fingerprint": batch[0]["fingerprint"], "materiality": 2,
         "affected_sectors": ["银行"], "affected_codes": ["600519", "000001"],
         "time_window": "1-3d", "needs_deep_review": False},
    ])
    queue = news_pipeline.read_json(news_pipeline.l1_queue_path(), [])
    graded = [e for e in queue if e["status"] == "graded"][0]
    assert graded["grade"]["affected_codes"] == ["600519", "000001"]


def test_submit_l2_grades_defaults_affected_codes_to_empty_list_for_backward_compat():
    _enqueue_one("央行宣布全面降准0.5个百分点")
    batch = news_pipeline.claim_l1_batch(
        "openclaw", now="2026-07-03T09:05:00+08:00",
    )
    news_pipeline.submit_l2_grades([
        {"fingerprint": batch[0]["fingerprint"], "materiality": 1,
         "affected_sectors": [], "time_window": "unknown",
         "needs_deep_review": False},
    ])
    queue = news_pipeline.read_json(news_pipeline.l1_queue_path(), [])
    graded = [e for e in queue if e["status"] == "graded"][0]
    assert graded["grade"]["affected_codes"] == []


def test_read_graded_news_matches_by_code():
    _enqueue_one("贵州茅台发布重大合作公告")
    _enqueue_one("某无关公司发布公告")
    result = _grade_batch(
        None,
        {
            "贵州茅台发布重大合作公告": {
                "materiality": 2, "affected_sectors": ["白酒"],
                "affected_codes": ["600519"], "time_window": "1-3d",
                "needs_deep_review": False,
            },
            "某无关公司发布公告": {
                "materiality": 1, "affected_sectors": [], "affected_codes": [],
                "time_window": "unknown", "needs_deep_review": False,
            },
        },
        now="2026-07-03T09:05:00+08:00",
    )
    assert result["graded"] == 2

    found = news_pipeline.read_graded_news(
        code="600519", now="2026-07-03T09:10:00+08:00",
    )
    assert found["status"] == "ok"
    assert len(found["items"]) == 1
    assert found["items"][0]["title"] == "贵州茅台发布重大合作公告"


def test_read_graded_news_matches_by_sector():
    _enqueue_one("白酒板块景气度上行")
    result = _grade_batch(
        None,
        {
            "白酒板块景气度上行": {
                "materiality": 2, "affected_sectors": ["白酒", "食品饮料"],
                "affected_codes": [], "time_window": "1-2w",
                "needs_deep_review": False,
            },
        },
        now="2026-07-03T09:05:00+08:00",
    )
    assert result["graded"] == 1

    found = news_pipeline.read_graded_news(
        sectors=["白酒"], now="2026-07-03T09:10:00+08:00",
    )
    assert found["status"] == "ok"
    assert len(found["items"]) == 1

    empty = news_pipeline.read_graded_news(
        sectors=["半导体"], now="2026-07-03T09:10:00+08:00",
    )
    assert empty["status"] == "empty"
    assert empty["items"] == []


def test_read_graded_news_returns_titles_urls_ranks_and_grading_fields():
    _enqueue_one("贵州茅台发布重大合作公告")
    _grade_batch(
        None,
        {
            "贵州茅台发布重大合作公告": {
                "materiality": 2, "affected_sectors": ["白酒"],
                "affected_codes": ["600519"], "time_window": "1-3d",
                "needs_deep_review": True,
            },
        },
        now="2026-07-03T09:05:00+08:00",
    )
    found = news_pipeline.read_graded_news(
        code="600519", now="2026-07-03T09:10:00+08:00",
    )
    item = found["items"][0]
    assert item["title"] == "贵州茅台发布重大合作公告"
    assert item["url"] == "https://example.gov.cn/a"
    assert item["source_rank"] == "S5"
    assert item["materiality"] == 2
    assert item["time_window"] == "1-3d"
    assert "graded_at" in item


def test_read_graded_news_excludes_entries_outside_day_window():
    _enqueue_one("贵州茅台发布重大合作公告")
    _grade_batch(
        None,
        {
            "贵州茅台发布重大合作公告": {
                "materiality": 2, "affected_sectors": [],
                "affected_codes": ["600519"], "time_window": "1-3d",
                "needs_deep_review": False,
            },
        },
        now="2026-06-20T09:05:00+08:00",
    )
    found = news_pipeline.read_graded_news(
        code="600519", days=7, now="2026-07-03T09:10:00+08:00",
    )
    assert found["status"] == "empty"
    assert found["items"] == []


def test_read_graded_news_sorts_by_materiality_desc_and_truncates():
    titles = [f"贵州茅台公告{i}" for i in range(10)]
    for title in titles:
        _enqueue_one(title)
    grades = {
        title: {
            "materiality": idx % 3,
            "affected_sectors": [], "affected_codes": ["600519"],
            "time_window": "unknown", "needs_deep_review": False,
        }
        for idx, title in enumerate(titles)
    }
    result = _grade_batch(None, grades, now="2026-07-03T09:05:00+08:00")
    assert result["graded"] == 10

    found = news_pipeline.read_graded_news(
        code="600519", limit=8, now="2026-07-03T09:10:00+08:00",
    )
    assert len(found["items"]) == 8
    materialities = [item["materiality"] for item in found["items"]]
    assert materialities == sorted(materialities, reverse=True)


def test_read_graded_news_empty_pool_reports_empty_status():
    found = news_pipeline.read_graded_news(
        code="600519", now="2026-07-03T09:10:00+08:00",
    )
    assert found["status"] == "empty"
    assert found["items"] == []


def test_read_graded_news_unavailable_when_queue_read_fails(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(news_pipeline, "read_json", _boom)
    found = news_pipeline.read_graded_news(code="600519")
    assert found["status"] == "unavailable"
    assert found["items"] == []


def test_read_graded_news_requires_code_or_sectors():
    with pytest.raises(ValueError):
        news_pipeline.read_graded_news()
