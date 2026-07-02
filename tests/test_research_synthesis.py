import json
import os

import pytest

import research_bus as bus
import research_synthesis as synthesis


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def config():
    return {
        "claim_ttl_minutes": 120,
        "max_attempts_per_role": 2,
        "budget": {"daily_char_budget": 0, "instructions_chars_estimate": 3000},
        "task_kinds": {
            "candidate_deep_dive": {
                "experts": ["evidence_auditor", "thesis_builder", "risk_redteam"],
                "priority": 70,
                "cooldown_days": 0,
                "pack_budget_chars": 24000,
                "pack_jobs": ["closing-triage"],
                "required_sections": [],
            },
        },
        "experts": {
            "evidence_auditor": {"max_output_chars": 4000},
            "thesis_builder": {"max_output_chars": 5000},
            "risk_redteam": {"max_output_chars": 5000},
        },
        "finding": {"max_summary_chars": 600, "max_finding_chars": 10000},
        "synthesis": {
            "veto_confidence": 0.7,
            "conflict_confidence": 0.6,
            "advance_min_support_confidence": 0.6,
            "escalation": {"enabled": False, "max_rounds": 1},
        },
    }


def _finding(task_id, role, stance, confidence):
    finding = {
        "schema": "research_finding_v1",
        "task_id": task_id,
        "role": role,
        "stance": stance,
        "confidence": confidence,
        "summary": f"{role} 判定 {stance}",
        "evidence_refs": ["fact_artifacts.closing-triage"],
        "risk_flags": [f"{role}_flag"] if stance == "oppose" else [],
    }
    if stance == "support":
        finding["counterevidence"] = [f"{role} 提出的反证"]
        finding["invalidation_conditions"] = ["跌破 20 日线"]
    if stance == "abstain":
        finding["abstain_reason"] = "证据不足"
        finding.pop("evidence_refs")
    return finding


def _run_task(config, stances):
    created = bus.enqueue_task(
        "candidate_deep_dive",
        {"code": "600519", "name": "贵州茅台"},
        reason="test_trigger",
        trading_date="2026-07-02",
        config=config,
    )
    task_id = created["task"]["id"]
    for _ in range(len(stances)):
        work = bus.claim_next_work("hermes", config=config)
        role = work["role"]
        stance, confidence = stances[role]
        result = bus.submit_finding(
            task_id, role, _finding(task_id, role, stance, confidence),
            config=config,
        )
        assert result["ok"], result
    return task_id


def test_unanimous_support_advances_with_gated_proposal(config):
    task_id = _run_task(config, {
        "evidence_auditor": ("support", 0.7),
        "thesis_builder": ("support", 0.8),
        "risk_redteam": ("neutral", 0.5),
    })
    result = synthesis.synthesize_task(task_id, config=config)
    assert result["ok"] is True
    record = result["synthesis"]
    assert record["verdict"] == "advance"
    assert record["policy_gate_required"] is True
    proposal = json.load(open(record["proposal_path"], encoding="utf-8"))
    assert proposal["policy_gate_required"] is True
    assert proposal["live_effect"].startswith("none_until")
    assert os.path.exists(record["report_path"])
    task = bus.find_task(task_id)
    assert task["status"] == "done"
    assert task["verdict"] == "advance"
    with open(bus.ledger_file(), encoding="utf-8") as handle:
        event = json.loads(handle.readline())
    assert event["event_type"] == "research.synthesized"


def test_risk_redteam_veto_rejects(config):
    task_id = _run_task(config, {
        "evidence_auditor": ("support", 0.9),
        "thesis_builder": ("support", 0.9),
        "risk_redteam": ("oppose", 0.8),
    })
    result = synthesis.synthesize_task(task_id, config=config)
    record = result["synthesis"]
    assert record["verdict"] == "rejected"
    assert record["basis"] == "risk_redteam_veto"
    assert "proposal_path" not in record
    assert bus.find_task(task_id)["status"] == "rejected"


def test_confident_disagreement_is_surfaced_not_averaged(config):
    task_id = _run_task(config, {
        "evidence_auditor": ("neutral", 0.5),
        "thesis_builder": ("support", 0.8),
        "risk_redteam": ("oppose", 0.65),
    })
    record = synthesis.synthesize_task(task_id, config=config)["synthesis"]
    assert record["verdict"] == "disputed"
    assert "proposal_path" not in record
    assert bus.find_task(task_id)["verdict"] == "disputed"


def test_all_abstain_yields_abstained(config):
    task_id = _run_task(config, {
        "evidence_auditor": ("abstain", 1.0),
        "thesis_builder": ("abstain", 1.0),
        "risk_redteam": ("abstain", 1.0),
    })
    record = synthesis.synthesize_task(task_id, config=config)["synthesis"]
    assert record["verdict"] == "abstained"
    assert bus.find_task(task_id)["status"] == "abstained"


def test_neutral_board_stays_watch_without_proposal(config):
    task_id = _run_task(config, {
        "evidence_auditor": ("neutral", 0.5),
        "thesis_builder": ("neutral", 0.4),
        "risk_redteam": ("neutral", 0.5),
    })
    record = synthesis.synthesize_task(task_id, config=config)["synthesis"]
    assert record["verdict"] == "watch"
    assert record["policy_gate_required"] is True
    assert "proposal_path" not in record


def test_bounded_escalation_reopens_conflicting_roles_once(config):
    config["synthesis"]["escalation"] = {"enabled": True, "max_rounds": 1}
    stances = {
        "evidence_auditor": ("neutral", 0.5),
        "thesis_builder": ("support", 0.8),
        "risk_redteam": ("oppose", 0.65),
    }
    task_id = _run_task(config, stances)

    first = synthesis.synthesize_task(task_id, config=config)
    assert first["ok"] is True
    assert first["escalated"] is True
    assert first["round"] == 1
    assert set(first["roles"]) == {"thesis_builder", "risk_redteam"}
    task = bus.find_task(task_id)
    assert task["status"] == "in_progress"
    assert task["escalation_round"] == 1
    assert task["roles"]["thesis_builder"]["status"] == "pending"
    assert task["roles"]["risk_redteam"]["status"] == "pending"

    for _ in range(2):
        work = bus.claim_next_work("openclaw", config=config)
        role = work["role"]
        stance, confidence = stances[role]
        assert bus.submit_finding(
            task_id, role, _finding(task_id, role, stance, confidence),
            config=config,
        )["ok"]

    second = synthesis.synthesize_task(task_id, config=config)
    assert second["synthesis"]["verdict"] == "disputed"
    assert bus.find_task(task_id)["status"] == "done"


def test_synthesis_requires_all_findings(config):
    created = bus.enqueue_task(
        "candidate_deep_dive",
        {"code": "600519"},
        reason="test",
        trading_date="2026-07-02",
        config=config,
    )
    result = synthesis.synthesize_task(created["task"]["id"], config=config)
    assert result["ok"] is False
    assert "missing" in result["error"]
