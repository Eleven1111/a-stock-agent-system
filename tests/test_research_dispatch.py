import json
import os

import pytest

import research_bus as bus
from paths import data_file, hermes_home
from scripts import research_dispatch as dispatch


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    config = {
        "claim_ttl_minutes": 120,
        "max_attempts_per_role": 2,
        "budget": {"daily_char_budget": 0, "instructions_chars_estimate": 3000},
        "task_kinds": {
            "candidate_deep_dive": {
                "experts": ["evidence_auditor", "thesis_builder", "risk_redteam"],
                "priority": 70,
                "cooldown_days": 3,
                "pack_budget_chars": 24000,
                "pack_jobs": ["closing-triage"],
                "required_sections": [],
            },
            "anomaly_review": {
                "experts": ["risk_redteam"],
                "priority": 85,
                "cooldown_days": 1,
                "pack_budget_chars": 16000,
                "pack_jobs": ["capital-flow"],
                "required_sections": [],
            },
            "postmortem": {
                "experts": ["evidence_auditor", "risk_redteam"],
                "priority": 60,
                "cooldown_days": 7,
                "pack_budget_chars": 20000,
                "pack_jobs": ["performance-daily"],
                "required_sections": [],
            },
            "user_request": {
                "experts": ["risk_redteam"],
                "priority": 95,
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
        "triggers": {
            "candidate_deep_dive": {"enabled": True, "top_k": 2},
            "postmortem": {
                "enabled": True,
                "min_final_loss_pct": -5.0,
                "max_per_day": 1,
            },
            "anomaly_review": {
                "enabled": True,
                "behavior_risk_levels": ["high", "critical"],
            },
        },
    }
    path = tmp_path / "research_committee.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("A_STOCK_RESEARCH_CONFIG", str(path))
    return config


def _write_candidate_pool(count=3):
    path = data_file("stock-triage", "candidate_pool_latest.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pool = {
        "status": "ready",
        "trading_date": "2026-07-02",
        "candidates": [
            {"code": f"60051{i}", "name": f"候选{i}", "score": 80 - i}
            for i in range(count)
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(pool, handle, ensure_ascii=False)


def _write_agent_state(behavior_level="normal", signals=None):
    path = os.path.join(hermes_home(), "agent_state", "agent_state_latest.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state = {
        "schema": "a_stock_agent_state_v1",
        "generated_at": "2026-07-02T15:45:00+00:00",
        "portfolio": {"positions": []},
        "recommendations": [],
        "signals": signals or [],
        "behavior_risk": {"level": behavior_level},
        "pending_settlements": [],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False)


def test_candidate_trigger_enqueues_top_k_once():
    _write_candidate_pool()
    _write_agent_state()

    first = dispatch.dispatch(trading_date="2026-07-02")
    assert first["enqueued"] == 2
    assert first["has_signal"] is True
    kinds = {task["kind"] for task in bus.load_tasks()}
    assert kinds == {"candidate_deep_dive"}

    second = dispatch.dispatch(trading_date="2026-07-02")
    assert second["enqueued"] == 0
    assert second["has_signal"] is False
    skip_reasons = {entry.get("skip_reason") for entry in second["results"]}
    assert skip_reasons == {"already_active"}


def test_behavior_risk_trigger_enqueues_anomaly_review():
    _write_agent_state(behavior_level="high")

    result = dispatch.dispatch(trading_date="2026-07-02")

    tasks = [task for task in bus.load_tasks() if task["kind"] == "anomaly_review"]
    assert len(tasks) == 1
    assert tasks[0]["subject_key"] == "behavior_risk_high"
    assert result["enqueued"] == 1


def test_postmortem_trigger_picks_worst_final_loss():
    _write_agent_state(signals=[
        {"code": "600001", "name": "亏损一", "settlement_status": "final",
         "t3_return_pct": -6.2},
        {"code": "600002", "name": "亏损二", "settlement_status": "final",
         "t3_return_pct": -9.8},
        {"code": "600003", "name": "盈利", "settlement_status": "final",
         "t3_return_pct": 4.0},
        {"code": "600004", "name": "未结算", "settlement_status": "pending",
         "t3_return_pct": -20.0},
    ])

    result = dispatch.dispatch(trading_date="2026-07-02")

    tasks = [task for task in bus.load_tasks() if task["kind"] == "postmortem"]
    assert len(tasks) == 1
    assert tasks[0]["subject_key"] == "600002"
    assert tasks[0]["reason"] == "final_loss_-9.8pct"
    assert result["enqueued"] == 1


def test_dispatch_is_silent_without_facts():
    result = dispatch.dispatch(trading_date="2026-07-02")
    assert result["enqueued"] == 0
    assert result["has_signal"] is False
    assert result["agent_state_available"] is False
    assert bus.load_tasks() == []


def test_manual_enqueue_via_cli(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [
        "research_dispatch.py",
        "--kind", "user_request",
        "--code", "600519",
        "--name", "贵州茅台",
        "--reason", "用户要求复核",
        "--trading-date", "2026-07-02",
    ])
    code = dispatch.main()
    output = json.loads(capsys.readouterr().out.strip())
    assert code == 0
    assert output["mode"] == "manual"
    assert output["results"][0]["enqueued"] is True
    task = bus.load_tasks()[0]
    assert task["kind"] == "user_request"
    assert task["reason"] == "用户要求复核"
