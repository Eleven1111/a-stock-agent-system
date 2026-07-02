import json
import os

import pytest

import evidence_pack
from paths import cron_output_dir, data_file, hermes_home


CONFIG = {
    "task_kinds": {
        "candidate_deep_dive": {
            "experts": ["risk_redteam"],
            "pack_budget_chars": 24000,
            "pack_jobs": ["closing-triage", "capital-flow"],
            "required_sections": ["agent_state", "fact_artifacts"],
        },
    },
    "experts": {"risk_redteam": {"max_output_chars": 5000}},
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


def _write_candidate_pool():
    path = data_file("stock-triage", "candidate_pool_latest.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pool = {
        "status": "ready",
        "trading_date": "2026-07-02",
        "candidates": [
            {"code": "600519", "name": "贵州茅台", "score": 82.5},
            {"code": "000001", "name": "平安银行", "score": 71.0},
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(pool, handle, ensure_ascii=False)


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
