import json
import os

import pytest

import evidence_pack
import news_pipeline
import research_bus as bus
import stock_intelligence
from paths import cron_output_dir, data_file, hermes_home


CONFIG = {
    "task_kinds": {
        "candidate_deep_dive": {
            "experts": ["risk_redteam"],
            "pack_budget_chars": 24000,
            "pack_jobs": ["closing-triage", "capital-flow"],
            "required_sections": ["agent_state", "fact_artifacts"],
        },
        "serenity_refresh": {
            "experts": ["deep_researcher"],
            "priority": 75,
            "cooldown_days": 90,
            "pack_budget_chars": 4000,
            "pack_jobs": [],
            "required_sections": [],
        },
    },
    "experts": {
        "risk_redteam": {"max_output_chars": 5000},
        "deep_researcher": {"max_output_chars": 3000},
    },
}

TASK = {
    "schema": "research_task_v1",
    "id": "rt-2026-07-02-candidate_deep_dive-600519",
    "kind": "candidate_deep_dive",
    "subject": {"code": "600519", "name": "贵州茅台"},
    "trading_date": "2026-07-02",
}


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    return tmp_path


def _write_agent_state():
    path = os.path.join(hermes_home(), "agent_state", "agent_state_latest.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state = {
        "schema": "a_stock_agent_state_v1",
        "generated_at": "2026-07-02T15:40:00+00:00",
        "portfolio": {"cash": 10000, "positions": [
            {"code": "600519", "name": "贵州茅台", "shares": 100, "cost": 1500.0},
        ]},
        "recommendations": [
            {"code": "600519", "name": "贵州茅台", "date": "2026-07-01",
             "action": "buy", "grade": "A", "confidence": "high",
             "outcome": "pending", "audit_blob": "x" * 500},
            {"code": "000001", "name": "平安银行", "date": "2026-06-30",
             "action": "watch", "grade": "B"},
        ],
        "signals": [
            {"code": "600519", "date": "2026-07-01", "action": "buy",
             "settlement_status": "pending"},
        ],
        "behavior_risk": {"level": "normal"},
        "pending_settlements": [{"code": "600519"}],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False)


def _write_artifact(job_id, trading_date="2026-07-02", stdout_tail="signal ok"):
    directory = os.path.join(cron_output_dir(), job_id)
    os.makedirs(directory, exist_ok=True)
    artifact = {
        "job_id": job_id,
        "trading_date": trading_date,
        "status": "ok",
        "finished_at": f"{trading_date}T15:36:00+08:00",
        "summary": {"status": "ok", "silent": False},
        "stdout_tail": stdout_tail,
    }
    with open(os.path.join(directory, "run-1.json"), "w", encoding="utf-8") as handle:
        json.dump(artifact, handle, ensure_ascii=False)


def _write_candidate_pool(*, sector=None):
    path = data_file("stock-triage", "candidate_pool_latest.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {"code": "600519", "name": "贵州茅台", "score": 82.5}
    if sector:
        entry["sector"] = sector
    pool = {
        "status": "ready",
        "trading_date": "2026-07-02",
        "candidates": [
            entry,
            {"code": "000001", "name": "平安银行", "score": 71.0},
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(pool, handle, ensure_ascii=False)


def _write_stock_intelligence(code, *, interactive_qa):
    payload = {
        "schema": stock_intelligence.SCHEMA,
        "code": code,
        "asof": "2026-07-02",
        "fetched_at": "2026-07-02T08:00:00+00:00",
        "lockups": {"history": [], "upcoming": []},
        "margin_trading": [],
        "holder_changes": [],
        "dragon_tiger": {
            "records": [], "seats": {"buy": [], "sell": []},
            "institution": {"net_amount_wan": 0.0},
        },
        "block_trades": [],
        "reports": [],
        "interactive_qa": interactive_qa,
        "dataset_status": {
            "lockups": {"status": "ok", "queried_asof": "2026-07-02", "latest_record_date": None},
            "margin_trading": {"status": "empty", "queried_asof": "2026-07-02", "latest_record_date": None},
            "holder_changes": {"status": "empty", "queried_asof": "2026-07-02", "latest_record_date": None},
            "dragon_tiger": {"status": "empty", "queried_asof": "2026-07-02", "latest_record_date": None},
            "block_trades": {"status": "empty", "queried_asof": "2026-07-02", "latest_record_date": None},
            "reports": {"status": "empty", "queried_asof": "2026-07-02", "latest_record_date": None},
            "interactive_qa": {
                "status": interactive_qa.get("status"),
                "queried_asof": "2026-07-02",
                "latest_record_date": None,
            },
        },
        "data_quality": {
            "status": "complete", "missing_datasets": [], "stale_datasets": [],
            "directional_ready": True, "errors": [],
        },
    }
    payload["risk_summary"] = stock_intelligence.assess_risks(payload)
    stock_intelligence.write_cache(payload)


def test_full_pack_is_ok_and_subject_scoped():
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    assert result["quality"]["status"] == "ok"
    payload = result["payload"]
    assert payload["agent_state"]["subject_position"]["code"] == "600519"
    recs = payload["agent_state"]["recommendations"]
    assert [rec["code"] for rec in recs] == ["600519"]
    assert "audit_blob" not in recs[0]
    jobs = [entry["job_id"] for entry in payload["fact_artifacts"]]
    assert jobs == ["closing-triage", "capital-flow"]
    assert payload["subject_data"]["candidate_entry"]["score"] == 82.5
    assert result["size_chars"] <= 24000


def test_pack_is_content_addressed_and_cached():
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()

    first = evidence_pack.build_pack(TASK, config=CONFIG)
    second = evidence_pack.build_pack(TASK, config=CONFIG)

    assert first["ref"] == second["ref"]
    assert first["cached"] is False
    assert second["cached"] is True
    stored = evidence_pack.load_pack(first["ref"])
    assert stored["payload"]["task_id"] == TASK["id"]


def test_missing_agent_state_fails_closed():
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    assert result["quality"]["status"] == "insufficient"
    assert "agent_state" in result["quality"]["missing"]


def test_missing_artifact_degrades_quality():
    _write_agent_state()
    _write_artifact("closing-triage")

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    assert result["quality"]["status"] == "degraded"
    flagged = [
        entry for entry in result["payload"]["fact_artifacts"]
        if entry.get("missing")
    ]
    assert flagged[0]["job_id"] == "capital-flow"


def test_stale_artifact_is_flagged():
    _write_agent_state()
    _write_artifact("closing-triage", trading_date="2026-07-01")
    _write_artifact("capital-flow")

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    assert result["quality"]["status"] == "degraded"
    triage = result["payload"]["fact_artifacts"][0]
    assert triage["stale"] is True


def test_budget_reduction_is_deterministic_and_bounded():
    _write_agent_state()
    _write_artifact("closing-triage", stdout_tail="x" * 1100)
    _write_artifact("capital-flow", stdout_tail="y" * 1100)
    _write_candidate_pool()
    tight = json.loads(json.dumps(CONFIG))
    tight["task_kinds"]["candidate_deep_dive"]["pack_budget_chars"] = 2500

    result = evidence_pack.build_pack(TASK, config=tight)
    stored = evidence_pack.load_pack(result["ref"])

    assert result["size_chars"] <= 2500
    assert stored["reductions"]
    assert "dropped_artifact_excerpts" in stored["reductions"]
    for entry in result["payload"]["fact_artifacts"]:
        assert "stdout_excerpt" not in entry


def test_missing_deep_research_pulls_serenity_refresh_and_flags_gap():
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    assert result["quality"]["deep_research_gap"] == "deep_research_missing_in_pack"
    tasks = bus.load_tasks()
    serenity_tasks = [t for t in tasks if t["kind"] == "serenity_refresh"]
    assert len(serenity_tasks) == 1
    assert serenity_tasks[0]["subject"]["code"] == "600519"
    assert serenity_tasks[0]["trigger"]["origin_task_id"] == TASK["id"]


def test_stale_deep_research_pulls_serenity_refresh_with_stale_reason(monkeypatch):
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()
    monkeypatch.setattr(
        "deep_research_cache.read_deep_research",
        lambda code, today=None: {
            "found": True, "code": code, "asof": "2026-01-01",
            "stale": True, "age_days": 180, "deep_score": 6.0,
        },
    )

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    assert result["quality"]["deep_research_gap"] == "deep_research_stale_in_pack"
    serenity_tasks = [
        t for t in bus.load_tasks() if t["kind"] == "serenity_refresh"
    ]
    assert len(serenity_tasks) == 1


def test_fresh_deep_research_does_not_pull_serenity_refresh(monkeypatch):
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()
    monkeypatch.setattr(
        "deep_research_cache.read_deep_research",
        lambda code, today=None: {
            "found": True, "code": code, "asof": "2026-07-01",
            "stale": False, "age_days": 1, "deep_score": 8.0,
        },
    )

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    assert "deep_research_gap" not in result["quality"]
    assert bus.load_tasks() == []


def test_serenity_refresh_task_never_pulls_another_serenity_refresh():
    serenity_task = {
        "schema": "research_task_v1",
        "id": "rt-2026-07-02-serenity_refresh-600519",
        "kind": "serenity_refresh",
        "subject": {"code": "600519", "name": "贵州茅台"},
        "trading_date": "2026-07-02",
    }

    result = evidence_pack.build_pack(serenity_task, config=CONFIG)

    assert "deep_research_gap" not in result["quality"]
    assert bus.load_tasks() == []


def test_repeated_pack_builds_do_not_duplicate_serenity_refresh_enqueue():
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()

    evidence_pack.build_pack(TASK, config=CONFIG)
    evidence_pack.build_pack(TASK, config=CONFIG)

    serenity_tasks = [
        t for t in bus.load_tasks() if t["kind"] == "serenity_refresh"
    ]
    assert len(serenity_tasks) == 1


def test_pack_surfaces_interactive_qa_with_reply_graded_b_and_question_lead_only():
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()
    _write_stock_intelligence("600519", interactive_qa={
        "market": "szse",
        "status": "ok",
        "rows": [
            {
                "date": "2026-07-01",
                "question": "公司产能利用率如何？",
                "reply": "目前产能利用率维持高位。",
                "has_reply": True,
                "platform": "szse_irm",
                "url": "https://irm.cninfo.com.cn/mobile/rmDetail?questionId=1",
            },
            {
                "date": "2026-06-28",
                "question": "股价为什么跌？",
                "reply": None,
                "has_reply": False,
                "platform": "szse_irm",
                "url": "https://irm.cninfo.com.cn/mobile/rmDetail?questionId=2",
            },
        ],
    })

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    items = result["payload"]["subject_data"]["interactive_qa"]["items"]
    assert result["payload"]["subject_data"]["interactive_qa"]["status"] == "ok"
    replied = next(item for item in items if item["has_reply"])
    unanswered = next(item for item in items if not item["has_reply"])
    assert replied["grade"] == "B"
    assert unanswered["grade"] == "attention_only"
    assert replied["url"].startswith("https://irm.cninfo.com.cn")


def test_pack_marks_interactive_qa_missing_when_cache_absent():
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    subject_data = result["payload"]["subject_data"]
    assert subject_data["interactive_qa"]["status"] == "missing"
    assert subject_data["interactive_qa"]["items"] == []


def test_pack_surfaces_sse_unavailable_status_without_fabricating_rows():
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()
    _write_stock_intelligence("600519", interactive_qa={
        "market": "sse",
        "status": "sse_unavailable",
        "rows": [],
        "error": {"source": "sse", "error": "uid not found"},
    })

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    interactive_qa = result["payload"]["subject_data"]["interactive_qa"]
    assert interactive_qa["status"] == "sse_unavailable"
    assert interactive_qa["items"] == []


# ---------------------------------------------------------------------------
# news_evidence: L2-graded news attached by code/sector for the subject stock
# ---------------------------------------------------------------------------

def _grade_news(title, *, materiality, affected_codes=None, affected_sectors=None,
                 now="2026-07-02T09:00:00+08:00"):
    l1_config = {
        "rank_weight": {"S5": 5},
        "materiality_keywords": {"critical": ["合作"]},
        "min_title_len": 4,
        "pass_threshold_score": 5,
    }
    scored = news_pipeline.score_item({
        "title": title,
        "url": "https://example.gov.cn/a",
        "source_id": "gov_test",
        "source_name": "测试官方源",
        "source_rank": "S5",
    }, l1_config)
    fresh, _ = news_pipeline.dedupe_items([scored])
    news_pipeline.enqueue_l1_items(fresh, now=now)
    batch = news_pipeline.claim_l1_batch("openclaw", now=now)
    fp = next(e["fingerprint"] for e in batch if e["title"] == title)
    news_pipeline.submit_l2_grades([{
        "fingerprint": fp,
        "materiality": materiality,
        "affected_sectors": affected_sectors or [],
        "affected_codes": affected_codes or [],
        "time_window": "1-3d",
        "needs_deep_review": False,
    }], now=now)


def test_pack_attaches_news_evidence_matched_by_code():
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()
    _grade_news("贵州茅台重大合作公告", materiality=2, affected_codes=["600519"])

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    news_evidence = result["payload"]["subject_data"]["news_evidence"]
    assert news_evidence["status"] == "ok"
    assert len(news_evidence["items"]) == 1
    item = news_evidence["items"][0]
    assert item["title"] == "贵州茅台重大合作公告"
    assert item["url"] == "https://example.gov.cn/a"
    assert item["materiality"] == 2


def test_pack_attaches_news_evidence_matched_by_sector():
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool(sector="白酒")
    _grade_news("白酒板块景气度合作上行", materiality=1, affected_sectors=["白酒"])

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    news_evidence = result["payload"]["subject_data"]["news_evidence"]
    assert news_evidence["status"] == "ok"
    assert len(news_evidence["items"]) == 1


def test_pack_news_evidence_status_empty_when_no_match():
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    news_evidence = result["payload"]["subject_data"]["news_evidence"]
    assert news_evidence["status"] == "empty"
    assert news_evidence["items"] == []


def test_pack_news_evidence_status_unavailable_on_read_failure(monkeypatch):
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()

    def _boom(**_kwargs):
        raise OSError("queue unreadable")

    monkeypatch.setattr("news_pipeline.read_graded_news", _boom)

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    news_evidence = result["payload"]["subject_data"]["news_evidence"]
    assert news_evidence["status"] == "unavailable"
    assert news_evidence["items"] == []


def test_pack_news_evidence_sorted_by_materiality_and_capped_at_eight():
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()
    for i in range(10):
        _grade_news(
            f"贵州茅台公告合作{i}", materiality=i % 4,
            affected_codes=["600519"],
            now=f"2026-07-02T09:{i:02d}:00+08:00",
        )

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    news_evidence = result["payload"]["subject_data"]["news_evidence"]
    assert news_evidence["status"] == "ok"
    assert len(news_evidence["items"]) == 8
    materialities = [item["materiality"] for item in news_evidence["items"]]
    assert materialities == sorted(materialities, reverse=True)


def test_pack_news_evidence_does_not_change_candidate_ranking_or_signals():
    _write_agent_state()
    _write_artifact("closing-triage")
    _write_artifact("capital-flow")
    _write_candidate_pool()
    _grade_news("贵州茅台重大合作公告", materiality=3, affected_codes=["600519"])

    result = evidence_pack.build_pack(TASK, config=CONFIG)

    assert result["payload"]["subject_data"]["candidate_entry"]["score"] == 82.5
