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


def _write_candidate_pool_with_structure_position(code, risk_flags):
    from paths import data_file

    path = data_file("stock-triage", "candidate_pool_latest.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pool = {
        "status": "ready",
        "trading_date": "2026-07-02",
        "candidates": [
            {
                "code": code, "name": "贵州茅台", "score": 82.5,
                "research_evidence": {
                    "schema": "research_evidence_v1",
                    "structure_position": {"available": True, "risk_flags": risk_flags},
                },
            },
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(pool, handle, ensure_ascii=False)


def test_risk_redteam_gains_structure_position_evidence_without_changing_verdict(config):
    """structure_position.risk_flags 并入 risk_redteam 证据文本，但 advance 结论不变
    （只作证据陈列，decide_verdict 只看 stance/confidence，不看 risk_flags）。"""
    import evidence_pack

    _write_candidate_pool_with_structure_position("600519", ["seg_end_divergence"])
    task_id = _run_task(config, {
        "evidence_auditor": ("support", 0.7),
        "thesis_builder": ("support", 0.8),
        "risk_redteam": ("neutral", 0.5),
    })
    task = bus.find_task(task_id)
    built = evidence_pack.build_pack(task, config=config)
    bus.update_task(task_id, {"evidence_pack_ref": built["ref"]})

    record = synthesis.synthesize_task(task_id, config=config)["synthesis"]

    assert record["verdict"] == "advance"
    assert "[结构位置]线段末端背驰" in record["risk_flags"]
    risk_entry = next(f for f in record["findings"] if f["role"] == "risk_redteam")
    assert risk_entry["stance"] == "neutral" and risk_entry["confidence"] == 0.5


def test_structure_position_flags_do_not_alter_risk_redteam_veto(config):
    """risk_redteam 一票否决逻辑不受 structure_position 证据陈列影响。"""
    import evidence_pack

    _write_candidate_pool_with_structure_position("600519", ["third_sell_structure"])
    task_id = _run_task(config, {
        "evidence_auditor": ("support", 0.9),
        "thesis_builder": ("support", 0.9),
        "risk_redteam": ("oppose", 0.8),
    })
    task = bus.find_task(task_id)
    built = evidence_pack.build_pack(task, config=config)
    bus.update_task(task_id, {"evidence_pack_ref": built["ref"]})

    record = synthesis.synthesize_task(task_id, config=config)["synthesis"]

    assert record["verdict"] == "rejected"
    assert record["basis"] == "risk_redteam_veto"
    assert "[结构位置]三卖后反弹未过中枢下沿" in record["risk_flags"]


def test_augment_no_op_without_risk_redteam_finding():
    findings = {"thesis_builder": {"risk_flags": []}}
    result = synthesis._augment_risk_redteam_with_structure_position(
        {"evidence_pack_ref": "sha256:does-not-exist"}, findings,
    )
    assert result is findings


def test_augment_dedupes_and_does_not_mutate_input(monkeypatch):
    monkeypatch.setattr(
        synthesis, "_structure_position_risk_flags",
        lambda task: ["[结构位置]线段末端背驰", "[结构位置]三卖后反弹未过中枢下沿"],
    )
    findings = {"risk_redteam": {"stance": "neutral", "risk_flags": ["[结构位置]线段末端背驰", "既有风险"]}}

    augmented = synthesis._augment_risk_redteam_with_structure_position({}, findings)

    assert augmented["risk_redteam"]["risk_flags"] == [
        "[结构位置]线段末端背驰", "既有风险", "[结构位置]三卖后反弹未过中枢下沿",
    ]
    assert augmented["risk_redteam"]["stance"] == "neutral"
    # 输入 findings 不被就地修改
    assert findings["risk_redteam"]["risk_flags"] == ["[结构位置]线段末端背驰", "既有风险"]


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
