import json
import os

import pytest

import research_bus
from paths import cron_output_dir


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture
def config():
    return {
        "claim_ttl_minutes": 120,
        "max_attempts_per_role": 2,
        "require_claim_fencing": True,
        "budget": {"daily_char_budget": 0, "instructions_chars_estimate": 100},
        "task_kinds": {
            "anomaly_review": {
                "experts": ["risk_redteam"],
                "priority": 85,
                "cooldown_days": 0,
                "pack_budget_chars": 16000,
                "pack_jobs": ["capital-flow"],
                "required_sections": ["fact_artifacts"],
            },
        },
        "experts": {"risk_redteam": {"max_output_chars": 5000}},
        "finding": {
            "max_summary_chars": 600,
            "max_finding_chars": 10000,
            "require_bound_evidence": True,
            "require_model_run_manifest": True,
            "manifest_max_age_minutes": 10,
        },
        "synthesis": {
            "veto_confidence": 0.7,
            "conflict_confidence": 0.6,
            "advance_min_support_confidence": 0.6,
            "escalation": {"enabled": False, "max_rounds": 1},
        },
        "triggers": {},
    }


def _write_artifact():
    directory = os.path.join(cron_output_dir(), "capital-flow")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "run-1.json"), "w", encoding="utf-8") as handle:
        json.dump({
            "job_id": "capital-flow",
            "trading_date": "2026-07-02",
            "status": "ok",
            "finished_at": "2026-07-02T15:36:00+08:00",
            "summary": {"status": "ok"},
        }, handle)


def _enqueue(config, *, now="2026-07-02T15:00:00+08:00"):
    return research_bus.enqueue_task(
        "anomaly_review",
        {"code": "000001", "name": "平安银行"},
        reason="behavior_risk",
        trading_date="2026-07-02",
        config=config,
        now=now,
    )["task"]


def _completed_payload(request):
    return {
        "status": "completed",
        "finding": {
            "schema": "research_finding_v1",
            "task_id": request.task_id,
            "role": request.role,
            "stance": "oppose",
            "confidence": 0.8,
            "summary": "资金证据与候选逻辑冲突，保持研究态。",
            "evidence_refs": ["fact_artifacts.capital-flow"],
            "risk_flags": ["capital_outflow"],
        },
        "tool_usage_summary": {
            "tools": ["read_evidence_pack"],
            "state_writes": [],
        },
        "model_usage": {"input_tokens": 100, "output_tokens": 50},
    }


def test_consume_once_runs_claim_to_review_only_synthesis(config):
    import research_consumer

    _write_artifact()
    task = _enqueue(config)
    result = research_consumer.consume_once(
        runtime="fake",
        worker="shadow-worker",
        turn=lambda request, pack: _completed_payload(request),
        model="fixture-model",
        config=config,
        now="2026-07-02T16:00:00+08:00",
    )

    assert result["status"] == "submitted", result
    assert result["task_id"] == task["id"]
    assert result["submit"]["ok"] is True
    assert result["synthesis"]["synthesis"]["verdict"] == "review_only"
    assert result["queue_before"]["oldest_active_age_seconds"] == 3600
    assert result["artifact_path"]
    with open(result["artifact_path"], encoding="utf-8") as handle:
        artifact = json.load(handle)
    assert artifact["schema"] == "research_consumer_run_v1"
    assert artifact["research_only"] is True
    assert artifact["trading_action"] == "none"


def test_consume_once_abstains_before_calling_model_when_pack_is_insufficient(config):
    import research_consumer

    task = _enqueue(config)
    called = []
    result = research_consumer.consume_once(
        runtime="fake",
        worker="shadow-worker",
        turn=lambda request, pack: called.append(True),
        model="fixture-model",
        config=config,
        now="2026-07-02T16:00:00+08:00",
    )

    assert result["status"] == "abstained"
    assert result["reason_codes"] == ["evidence_pack_insufficient"]
    assert called == []
    assert research_bus.find_task(task["id"])["status"] == "abstained"


def test_consume_once_maps_timeout_to_retryable_error(config):
    import research_consumer

    _write_artifact()
    task = _enqueue(config)

    def timeout(request, pack):
        raise TimeoutError("deadline")

    result = research_consumer.consume_once(
        runtime="fake",
        worker="shadow-worker",
        turn=timeout,
        model="fixture-model",
        config=config,
        now="2026-07-02T16:00:00+08:00",
    )

    assert result["status"] == "retryable_error"
    assert result["reason_codes"] == ["deadline_exceeded"]
    assert research_bus.find_task(task["id"])["roles"]["risk_redteam"]["status"] == "pending"


def test_consume_once_blocks_fact_plane_write(config):
    import research_consumer

    _write_artifact()
    task = _enqueue(config)

    def overreaching(request, pack):
        payload = _completed_payload(request)
        payload["tool_usage_summary"]["state_writes"] = ["state/signal_ledger.jsonl"]
        return payload

    result = research_consumer.consume_once(
        runtime="fake",
        worker="shadow-worker",
        turn=overreaching,
        model="fixture-model",
        config=config,
        now="2026-07-02T16:00:00+08:00",
    )

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["forbidden_state_write"]
    assert research_bus.find_task(task["id"])["status"] == "failed"


def test_consume_once_without_runtime_turn_does_not_claim(config):
    import research_consumer

    task = _enqueue(config)
    result = research_consumer.consume_once(
        runtime="hermes",
        worker="shadow-worker",
        turn=None,
        model="",
        config=config,
        now="2026-07-02T16:00:00+08:00",
    )

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["runtime_turn_unconfigured"]
    assert research_bus.find_task(task["id"])["status"] == "pending"


def test_consume_once_without_model_version_does_not_claim(config):
    import research_consumer

    task = _enqueue(config)
    result = research_consumer.consume_once(
        runtime="hermes",
        worker="shadow-worker",
        turn=lambda request, pack: _completed_payload(request),
        model="",
        config=config,
        now="2026-07-02T16:00:00+08:00",
    )

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["model_version_unconfigured"]
    assert research_bus.find_task(task["id"])["status"] == "pending"


def test_consume_once_does_not_steal_an_active_lease(config):
    import research_consumer

    _write_artifact()
    task = _enqueue(config)
    claimed = research_bus.claim_next_work(
        "worker-a", config=config, now="2026-07-02T15:30:00+08:00"
    )
    called = []
    result = research_consumer.consume_once(
        runtime="fake",
        worker="worker-b",
        turn=lambda request, pack: called.append(True),
        model="fixture-model",
        config=config,
        now="2026-07-02T16:00:00+08:00",
    )

    assert result["status"] == "idle"
    assert called == []
    active = research_bus.find_task(task["id"])["roles"]["risk_redteam"]
    assert active["claimed_by"] == "worker-a"
    assert active["claim_id"] == claimed["claim_id"]


def test_consume_once_replay_is_idle_after_terminal_submission(config):
    import research_consumer

    _write_artifact()
    _enqueue(config)
    called = []

    def turn(request, pack):
        called.append(request.claim_id)
        return _completed_payload(request)

    first = research_consumer.consume_once(
        runtime="fake",
        worker="shadow-worker",
        turn=turn,
        model="fixture-model",
        config=config,
        now="2026-07-02T16:00:00+08:00",
    )
    second = research_consumer.consume_once(
        runtime="fake",
        worker="shadow-worker",
        turn=turn,
        model="fixture-model",
        config=config,
        now="2026-07-02T16:01:00+08:00",
    )

    assert first["status"] == "submitted"
    assert second["status"] == "idle"
    assert len(called) == 1


def test_consume_once_releases_claim_when_bus_rejects_model_finding(config):
    import research_consumer

    _write_artifact()
    task = _enqueue(config)

    def incomplete_support(request, pack):
        payload = _completed_payload(request)
        payload["finding"]["stance"] = "support"
        return payload

    result = research_consumer.consume_once(
        runtime="fake",
        worker="shadow-worker",
        turn=incomplete_support,
        model="fixture-model",
        config=config,
        now="2026-07-02T16:00:00+08:00",
    )

    assert result["status"] == "blocked"
    assert "submission_rejected" in result["reason_codes"]
    role = research_bus.find_task(task["id"])["roles"]["risk_redteam"]
    assert role["status"] == "failed"
    assert "claim_id" not in role
