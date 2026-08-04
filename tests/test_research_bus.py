import json
import os
from datetime import datetime

import pytest

import research_bus as bus


@pytest.fixture
def config():
    return {
        "claim_ttl_minutes": 120,
        "max_attempts_per_role": 2,
        "budget": {
            "daily_char_budget": 400000,
            "instructions_chars_estimate": 3000,
        },
        "task_kinds": {
            "candidate_deep_dive": {
                "experts": ["evidence_auditor", "thesis_builder", "risk_redteam"],
                "priority": 70,
                "cooldown_days": 3,
                "pack_budget_chars": 24000,
                "pack_jobs": ["closing-triage"],
                "required_sections": ["agent_state"],
            },
            "anomaly_review": {
                "experts": ["risk_redteam"],
                "priority": 85,
                "cooldown_days": 1,
                "pack_budget_chars": 16000,
                "pack_jobs": ["capital-flow"],
                "required_sections": [],
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
            "evidence_auditor": {"max_output_chars": 4000},
            "thesis_builder": {"max_output_chars": 5000},
            "risk_redteam": {"max_output_chars": 5000},
            "deep_researcher": {"max_output_chars": 3000},
        },
        "finding": {"max_summary_chars": 600, "max_finding_chars": 10000},
        "synthesis": {
            "veto_confidence": 0.7,
            "conflict_confidence": 0.6,
            "advance_min_support_confidence": 0.6,
            "escalation": {"enabled": False, "max_rounds": 1},
        },
    }


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    return tmp_path


def _enqueue(config, **overrides):
    params = {
        "kind": "candidate_deep_dive",
        "subject": {"code": "600519", "name": "贵州茅台"},
        "reason": "candidate_pool_top",
        "trading_date": "2026-07-02",
        "config": config,
        "now": "2026-07-02T15:50:00",
    }
    params.update(overrides)
    kind = params.pop("kind")
    subject = params.pop("subject")
    return bus.enqueue_task(kind, subject, **params)


def _valid_finding(task, role, stance="support"):
    finding = {
        "schema": "research_finding_v1",
        "task_id": task["id"],
        "role": role,
        "stance": stance,
        "confidence": 0.8,
        "summary": "测试结论",
        "evidence_refs": ["fact_artifacts.closing-triage"],
        "risk_flags": [],
    }
    if stance == "support":
        finding["counterevidence"] = ["外资连续三日净流出"]
        finding["invalidation_conditions"] = ["跌破 20 日线"]
    if stance == "abstain":
        finding["abstain_reason"] = "证据包缺少关键数据"
        finding.pop("evidence_refs")
    return finding


def _submit(work, finding, *, config):
    return bus.submit_finding(
        work["task"]["id"], work["role"], finding,
        claim_id=work["claim_id"], config=config,
    )


def test_enqueue_creates_task_with_expert_plan(config):
    result = _enqueue(config)
    assert result["enqueued"] is True
    task = result["task"]
    assert task["id"] == "rt-2026-07-02-candidate_deep_dive-600519"
    assert task["expert_plan"] == [
        "evidence_auditor", "thesis_builder", "risk_redteam",
    ]
    assert set(task["roles"]) == set(task["expert_plan"])
    assert task["budget"]["estimated_chars"] > 24000


def test_enqueue_default_created_at_is_timezone_aware(config):
    task = bus.enqueue_task(
        "candidate_deep_dive",
        {"code": "600519", "name": "贵州茅台"},
        reason="timezone_contract",
        trading_date="2026-07-02",
        config=config,
    )["task"]

    created_at = datetime.fromisoformat(task["created_at"])
    assert created_at.tzinfo is not None
    assert created_at.utcoffset() is not None


def test_enqueue_dedupes_active_subject(config):
    assert _enqueue(config)["enqueued"] is True
    repeat = _enqueue(config, reason="second_trigger")
    assert repeat["enqueued"] is False
    assert repeat["reason"] == "already_active"


def test_enqueue_respects_cooldown_and_force(config):
    first = _enqueue(config)
    bus.update_task(first["task"]["id"], {"status": "done"})
    repeat = _enqueue(config, trading_date="2026-07-03")
    assert repeat["enqueued"] is False
    assert repeat["reason"] == "cooldown"
    forced = _enqueue(config, trading_date="2026-07-03", force=True)
    assert forced["enqueued"] is True


def test_claim_orders_by_priority_and_marks_in_progress(config):
    _enqueue(config)
    _enqueue(
        config,
        kind="anomaly_review",
        subject={"code": "000001"},
        reason="behavior_risk",
    )
    work = bus.claim_next_work("openclaw", config=config, now="2026-07-02T16:00:00")
    assert work["task"]["kind"] == "anomaly_review"
    assert work["role"] == "risk_redteam"
    task = bus.find_task(work["task"]["id"])
    assert task["status"] == "in_progress"
    assert task["roles"]["risk_redteam"]["claimed_by"] == "openclaw"
    assert task["roles"]["risk_redteam"]["attempts"] == 1


def test_claim_expires_stale_leases(config):
    _enqueue(config, kind="anomaly_review", subject={"code": "000001"})
    first = bus.claim_next_work("hermes", config=config, now="2026-07-02T16:00:00")
    assert first is not None
    again = bus.claim_next_work("openclaw", config=config, now="2026-07-02T16:30:00")
    assert again is None
    reclaimed = bus.claim_next_work(
        "openclaw", config=config, now="2026-07-02T18:30:00",
    )
    assert reclaimed is not None
    assert reclaimed["task"]["roles"]["risk_redteam"]["claimed_by"] == "openclaw"


def test_aware_clock_can_expire_legacy_naive_claim(config):
    _enqueue(config, kind="anomaly_review", subject={"code": "000001"})
    first = bus.claim_next_work(
        "hermes",
        config=config,
        now="2026-07-02T16:00:00",
    )
    assert first is not None

    reclaimed = bus.claim_next_work(
        "openclaw",
        config=config,
        now="2026-07-02T18:30:00+08:00",
    )

    assert reclaimed is not None
    assert reclaimed["task"]["roles"]["risk_redteam"]["claimed_by"] == "openclaw"


def test_expired_lease_at_attempt_limit_fails_task_instead_of_sticking(config):
    limited = dict(config)
    limited["max_attempts_per_role"] = 1
    _enqueue(limited, kind="anomaly_review", subject={"code": "000001"})
    first = bus.claim_next_work(
        "hermes", config=limited, now="2026-07-02T16:00:00",
    )
    assert first is not None
    assert bus.claim_next_work(
        "openclaw", config=limited, now="2026-07-02T19:00:00",
    ) is None
    task = bus.find_task(first["task"]["id"])
    assert task["status"] == "failed"
    assert task["roles"]["risk_redteam"]["status"] == "failed"


def test_reclaimed_lease_fences_old_worker_submission(config):
    _enqueue(config, kind="anomaly_review", subject={"code": "000001"})
    old = bus.claim_next_work("hermes", config=config, now="2026-07-02T16:00:00")
    new = bus.claim_next_work("openclaw", config=config, now="2026-07-02T19:00:00")
    finding = _valid_finding(new["task"], new["role"], stance="neutral")
    stale = bus.submit_finding(
        old["task"]["id"], old["role"], finding,
        worker="hermes", claim_id=old["claim_id"], config=config,
    )
    assert stale["ok"] is False
    assert "claim_id does not match" in stale["errors"][0]
    accepted = bus.submit_finding(
        new["task"]["id"], new["role"], finding,
        worker="openclaw", claim_id=new["claim_id"], config=config,
    )
    assert accepted["ok"] is True


def test_submit_cas_rechecks_lease_before_publishing_finding(
    config, monkeypatch,
):
    _enqueue(config, kind="anomaly_review", subject={"code": "000001"})
    old = bus.claim_next_work("hermes", config=config, now="2026-07-02T16:00:00")
    stale_finding = _valid_finding(
        old["task"], old["role"], stance="neutral",
    )
    stale_finding["summary"] = "旧 lease 的过期结论"

    original_mutate_json = bus.mutate_json
    reclaimed = {}
    intercepting = False

    def _reclaim_before_submit_cas(path, mutator, default=None):
        nonlocal intercepting
        if not intercepting:
            intercepting = True
            reclaimed.update(
                bus.claim_next_work(
                    "openclaw",
                    config=config,
                    now="2026-07-02T19:00:00",
                )
                or {}
            )
        return original_mutate_json(path, mutator, default)

    monkeypatch.setattr(bus, "mutate_json", _reclaim_before_submit_cas)
    stale = bus.submit_finding(
        old["task"]["id"],
        old["role"],
        stale_finding,
        worker="hermes",
        claim_id=old["claim_id"],
        config=config,
    )

    assert reclaimed["claim_id"] != old["claim_id"]
    assert stale["ok"] is False
    assert "claim_id does not match" in stale["errors"][0]
    current = bus.find_task(old["task"]["id"])
    role_state = current["roles"][old["role"]]
    assert role_state["status"] == "claimed"
    assert role_state["claim_id"] == reclaimed["claim_id"]
    assert role_state["claimed_by"] == "openclaw"
    assert not os.path.exists(
        os.path.join(bus.board_dir(old["task"]["id"]), f"{old['role']}.json")
    )

    monkeypatch.setattr(bus, "mutate_json", original_mutate_json)
    valid_finding = _valid_finding(
        reclaimed["task"], reclaimed["role"], stance="neutral",
    )
    valid_finding["summary"] = "新 lease 的有效结论"
    accepted = bus.submit_finding(
        reclaimed["task"]["id"],
        reclaimed["role"],
        valid_finding,
        worker="openclaw",
        claim_id=reclaimed["claim_id"],
        config=config,
    )
    assert accepted["ok"] is True
    with open(accepted["board_path"], encoding="utf-8") as handle:
        assert json.load(handle)["summary"] == "新 lease 的有效结论"


def test_turn_stream_records_claim_and_submit(config):
    _enqueue(config, kind="anomaly_review", subject={"code": "000001"})
    work = bus.claim_next_work("worker-a", config=config)
    finding = _valid_finding(work["task"], work["role"], stance="neutral")
    assert bus.submit_finding(
        work["task"]["id"], work["role"], finding, worker="worker-a",
        claim_id=work["claim_id"], config=config,
    )["ok"]
    with open(bus.turns_file(work["task"]["id"]), encoding="utf-8") as handle:
        events = [json.loads(line) for line in handle if line.strip()]
    assert [event["event_type"] for event in events] == [
        "research.turn.claimed", "research.turn.submitted",
    ]
    assert all(event["event_id"] for event in events)


def test_duplicate_submission_is_idempotent_for_same_claim(config):
    _enqueue(config, kind="anomaly_review", subject={"code": "000001"})
    work = bus.claim_next_work("hermes", config=config)
    finding = _valid_finding(work["task"], work["role"], stance="neutral")
    first = _submit(work, finding, config=config)
    second = bus.submit_finding(
        work["task"]["id"], work["role"], finding,
        worker="hermes", claim_id=work["claim_id"], config=config,
    )
    assert first["ok"] is True
    assert second["ok"] is True
    assert second["idempotent"] is True


def test_submit_rejects_fact_plane_directive(config):
    _enqueue(config, kind="anomaly_review", subject={"code": "000001"})
    work = bus.claim_next_work("hermes", config=config)
    finding = _valid_finding(work["task"], work["role"], stance="neutral")
    finding["place_order"] = True
    result = _submit(work, finding, config=config)
    assert result["ok"] is False
    assert "fact_plane_directive" in result["errors"]


def test_submit_rejects_support_without_counterevidence(config):
    _enqueue(config, kind="anomaly_review", subject={"code": "000001"})
    work = bus.claim_next_work("hermes", config=config)
    finding = _valid_finding(work["task"], work["role"])
    finding.pop("counterevidence")
    result = _submit(work, finding, config=config)
    assert result["ok"] is False
    assert any("counterevidence" in error for error in result["errors"])


def test_submit_writes_board_and_completes_single_role_task(config):
    _enqueue(config, kind="anomaly_review", subject={"code": "000001"})
    work = bus.claim_next_work("hermes", config=config)
    finding = _valid_finding(work["task"], work["role"])
    result = _submit(work, finding, config=config)
    assert result["ok"] is True
    assert result["all_roles_done"] is True
    assert result["task_status"] == "ready_to_synthesize"
    with open(result["board_path"], encoding="utf-8") as handle:
        assert json.load(handle)["stance"] == "support"


def test_submit_requires_claim_before_finding(config):
    created = _enqueue(config, kind="anomaly_review", subject={"code": "000001"})
    finding = _valid_finding(created["task"], "risk_redteam")
    result = bus.submit_finding(
        created["task"]["id"], "risk_redteam", finding, config=config,
    )
    assert result["ok"] is False


def test_multi_role_task_synthesizes_only_after_all_roles(config):
    created = _enqueue(config)
    task_id = created["task"]["id"]
    for _ in range(3):
        work = bus.claim_next_work("hermes", config=config)
        finding = _valid_finding(work["task"], work["role"], stance="neutral")
        result = _submit(work, finding, config=config)
        assert result["ok"] is True
    assert bus.find_task(task_id)["status"] == "ready_to_synthesize"


def test_budget_exhaustion_defers_claim(config):
    tight = json.loads(json.dumps(config))
    tight["budget"]["daily_char_budget"] = 1000
    _enqueue(tight)
    work = bus.claim_next_work("hermes", config=tight)
    assert work is None
    task = bus.load_tasks()[0]
    assert task["deferred_reason"] == "daily_budget_exhausted"
    assert task["status"] == "pending"


def test_fail_role_retries_then_fails_task(config):
    _enqueue(config, kind="anomaly_review", subject={"code": "000001"})
    work = bus.claim_next_work("hermes", config=config)
    task_id = work["task"]["id"]
    first = bus.fail_role(
        task_id, "risk_redteam", "timeout", claim_id=work["claim_id"], config=config,
    )
    assert first["role_status"] == "pending"
    work = bus.claim_next_work("hermes", config=config)
    assert work is not None
    second = bus.fail_role(
        task_id, "risk_redteam", "timeout", claim_id=work["claim_id"], config=config,
    )
    assert second["role_status"] == "failed"
    assert bus.find_task(task_id)["status"] == "failed"
    assert bus.claim_next_work("hermes", config=config) is None


def test_budget_ledger_records_reservation_and_usage(config):
    created = _enqueue(config, kind="anomaly_review", subject={"code": "000001"})
    work = bus.claim_next_work("hermes", config=config)
    finding = _valid_finding(work["task"], work["role"], stance="neutral")
    _submit(work, finding, config=config)
    usage = json.load(open(bus.budget_file("2026-07-02"), encoding="utf-8"))
    assert usage["reserved_chars"] > 0
    assert usage["entries"][0]["task_id"] == created["task"]["id"]
    assert usage["actuals"][0]["actual_chars"] > 0


def test_ledger_append_and_queue_summary(config):
    _enqueue(config)
    bus.append_ledger_event({"event_type": "research.enqueued", "task_id": "x"})
    with open(bus.ledger_file(), encoding="utf-8") as handle:
        lines = handle.read().strip().splitlines()
    assert json.loads(lines[0])["event_type"] == "research.enqueued"
    summary = bus.queue_summary()
    assert summary["total"] == 1
    assert summary["by_status"]["pending"] == 1
    assert summary["active"][0]["kind"] == "candidate_deep_dive"


def test_estimate_includes_pack_and_all_roles(config):
    estimate = bus.estimate_task_chars("candidate_deep_dive", config)
    assert estimate == 24000 + (3000 + 4000) + (3000 + 5000) + (3000 + 5000)
    assert os.path.basename(bus.queue_file()) == "research_tasks.json"


def test_serenity_refresh_enqueues_single_role_task(config):
    result = bus.enqueue_task(
        "serenity_refresh",
        {"code": "600519", "name": "贵州茅台"},
        reason="missing_cache",
        trading_date="2026-07-02",
        config=config,
    )
    assert result["enqueued"] is True
    task = result["task"]
    assert task["expert_plan"] == ["deep_researcher"]
    assert task["priority"] == 75


def test_serenity_refresh_dedupes_same_code(config):
    first = bus.enqueue_task(
        "serenity_refresh",
        {"code": "600519", "name": "贵州茅台"},
        reason="missing_cache",
        trading_date="2026-07-02",
        config=config,
    )
    assert first["enqueued"] is True
    repeat = bus.enqueue_task(
        "serenity_refresh",
        {"code": "600519", "name": "贵州茅台"},
        reason="demand_pulled",
        trading_date="2026-07-02",
        config=config,
    )
    assert repeat["enqueued"] is False
    assert repeat["reason"] == "already_active"


def _write_fresh_cache(monkeypatch, tmp_path, code, asof):
    import deep_research_cache

    monkeypatch.setattr(
        deep_research_cache, "cache_file",
        lambda c: str(tmp_path / f"cache-{c}.json"),
    )
    deep_research_cache.write_deep_research(
        code, "贵州茅台", {"total": 80, "rating": "buy"}, asof=asof,
    )
    return deep_research_cache


def test_submit_serenity_refresh_requires_fresh_cache(config, monkeypatch, tmp_path):
    import deep_research_cache

    monkeypatch.setattr(
        deep_research_cache, "cache_file",
        lambda c: str(tmp_path / f"cache-{c}.json"),
    )
    created = bus.enqueue_task(
        "serenity_refresh",
        {"code": "600519", "name": "贵州茅台"},
        reason="missing_cache",
        trading_date="2026-07-02",
        config=config,
    )
    task_id = created["task"]["id"]
    work = bus.claim_next_work("openclaw", config=config)
    finding = {
        "schema": "research_finding_v1",
        "task_id": task_id,
        "role": "deep_researcher",
        "stance": "neutral",
        "confidence": 0.8,
        "summary": "深研已完成但未写缓存",
        "evidence_refs": ["deep_research_cache:600519:2026-07-02"],
    }
    result = _submit(work, finding, config=config)
    assert result["ok"] is False
    assert any("cache" in error for error in result["errors"])
    assert work["task"]["roles"]["deep_researcher"]["status"] != "done"


def test_submit_serenity_refresh_accepts_after_fresh_cache_write(
    config, monkeypatch, tmp_path,
):
    _write_fresh_cache(monkeypatch, tmp_path, "600519", "2026-07-02")
    created = bus.enqueue_task(
        "serenity_refresh",
        {"code": "600519", "name": "贵州茅台"},
        reason="missing_cache",
        trading_date="2026-07-02",
        config=config,
    )
    task_id = created["task"]["id"]
    work = bus.claim_next_work("openclaw", config=config)
    finding = {
        "schema": "research_finding_v1",
        "task_id": task_id,
        "role": "deep_researcher",
        "stance": "neutral",
        "confidence": 0.8,
        "summary": "深研完成，缓存已写入",
        "evidence_refs": ["deep_research_cache:600519:2026-07-02"],
    }
    result = _submit(work, finding, config=config)
    assert result["ok"] is True
    assert result["all_roles_done"] is True


def test_submit_serenity_refresh_rejects_cache_older_than_trading_date(
    config, monkeypatch, tmp_path,
):
    _write_fresh_cache(monkeypatch, tmp_path, "600519", "2026-06-30")
    created = bus.enqueue_task(
        "serenity_refresh",
        {"code": "600519", "name": "贵州茅台"},
        reason="missing_cache",
        trading_date="2026-07-02",
        config=config,
    )
    task_id = created["task"]["id"]
    work = bus.claim_next_work("openclaw", config=config)
    finding = {
        "schema": "research_finding_v1",
        "task_id": task_id,
        "role": "deep_researcher",
        "stance": "neutral",
        "confidence": 0.8,
        "summary": "深研完成但缓存过期于交易日之前",
        "evidence_refs": ["deep_research_cache:600519:2026-06-30"],
    }
    result = _submit(work, finding, config=config)
    assert result["ok"] is False
    assert any("predates" in error for error in result["errors"])


def test_submit_serenity_refresh_abstain_bypasses_cache_check(config):
    created = bus.enqueue_task(
        "serenity_refresh",
        {"code": "600519", "name": "贵州茅台"},
        reason="missing_cache",
        trading_date="2026-07-02",
        config=config,
    )
    task_id = created["task"]["id"]
    work = bus.claim_next_work("openclaw", config=config)
    finding = {
        "schema": "research_finding_v1",
        "task_id": task_id,
        "role": "deep_researcher",
        "stance": "abstain",
        "confidence": 1.0,
        "summary": "标的停牌，无法深研",
        "abstain_reason": "标的停牌",
    }
    result = _submit(work, finding, config=config)
    assert result["ok"] is True
