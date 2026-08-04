import json
import os

import pytest

import research_bus as bus
from paths import cron_output_dir
from scripts import expert_runner as runner


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
            "anomaly_review": {
                "experts": ["risk_redteam"],
                "priority": 85,
                "cooldown_days": 0,
                "pack_budget_chars": 16000,
                "pack_jobs": ["capital-flow"],
                "required_sections": ["fact_artifacts"],
            },
        },
        "experts": {
            "risk_redteam": {
                "profile": "skills/research-committee/experts/risk_redteam.md",
                "max_output_chars": 5000,
            },
        },
        "finding": {"max_summary_chars": 600, "max_finding_chars": 10000},
        "synthesis": {
            "veto_confidence": 0.7,
            "conflict_confidence": 0.6,
            "advance_min_support_confidence": 0.6,
            "escalation": {"enabled": False, "max_rounds": 1},
        },
        "triggers": {},
    }
    path = tmp_path / "research_committee.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("A_STOCK_RESEARCH_CONFIG", str(path))
    return config


def _run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr("sys.argv", ["expert_runner.py", *argv])
    code = runner.main()
    output = capsys.readouterr().out.strip()
    return code, json.loads(output)


def _write_artifact(job_id):
    directory = os.path.join(cron_output_dir(), job_id)
    os.makedirs(directory, exist_ok=True)
    artifact = {
        "job_id": job_id,
        "trading_date": "2026-07-02",
        "status": "ok",
        "finished_at": "2026-07-02T15:36:00+08:00",
        "summary": {"status": "ok"},
        "stdout_tail": "capital flow normal",
    }
    with open(os.path.join(directory, "run-1.json"), "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False)


def _enqueue_anomaly(config):
    return bus.enqueue_task(
        "anomaly_review",
        {"code": "000001", "name": "平安银行"},
        reason="behavior_risk",
        trading_date="2026-07-02",
        config=config,
    )["task"]


def _trusted_approval_path(tmp_path, name="approval.json"):
    root = tmp_path / "state" / "approvals" / "research-committee"
    root.mkdir(parents=True, exist_ok=True)
    return root / name


def test_next_reports_idle_on_empty_queue(monkeypatch, capsys):
    code, output = _run_cli(monkeypatch, capsys, "next", "--worker", "openclaw")
    assert code == 0
    assert output["status"] == "idle"


def test_full_cycle_work_order_submit_and_synthesis(
    monkeypatch, capsys, tmp_path, config_file,
):
    _write_artifact("capital-flow")
    task = _enqueue_anomaly(config_file)

    code, order = _run_cli(monkeypatch, capsys, "next", "--worker", "openclaw")
    assert code == 0
    assert order["schema"] == "research_work_order_v1"
    assert order["task_id"] == task["id"]
    assert order["role"] == "risk_redteam"
    assert order["evidence_pack"]["quality"]["status"] == "ok"
    assert order["output_contract"]["schema"] == "research_finding_v1"
    assert order["instructions"].strip()
    assert order["claim_id"] in order["submit_command"]
    assert order["claim_id"] in order["abstain_command"]

    refreshed = bus.find_task(task["id"])
    assert refreshed["evidence_pack_ref"] == order["evidence_pack_ref"]

    finding = {
        "schema": "research_finding_v1",
        "task_id": task["id"],
        "role": "risk_redteam",
        "stance": "oppose",
        "confidence": 0.8,
        "summary": "资金面证据与候选逻辑冲突，行为风险偏高。",
        "evidence_refs": ["fact_artifacts.capital-flow"],
        "risk_flags": ["capital_outflow"],
    }
    finding_path = tmp_path / "finding.json"
    finding_path.write_text(json.dumps(finding, ensure_ascii=False), encoding="utf-8")
    approval_path = _trusted_approval_path(tmp_path)
    approval_path.write_text(json.dumps({
        "schema": "research_finding_approval_v1",
        "task_id": task["id"],
        "role": "risk_redteam",
        "claim_id": order["claim_id"],
        "finding_sha256": runner.agent_evidence.finding_sha256(finding),
        "status": "approved",
        "reviewer": "risk-owner",
        "approved_at": "2026-07-02T16:00:00+00:00",
    }), encoding="utf-8")

    code, result = _run_cli(
        monkeypatch, capsys,
        "submit", "--task", task["id"], "--role", "risk_redteam",
        "--file", str(finding_path), "--worker", "openclaw",
        "--claim-id", order["claim_id"],
        "--model", "fixture-model", "--approval-file", str(approval_path),
    )
    assert code == 0
    assert result["ok"] is True
    assert result["all_roles_done"] is True
    assert result["synthesis"]["synthesis"]["verdict"] == "rejected"
    assert os.path.exists(result["synthesis"]["synthesis"]["report_path"])
    assert bus.find_task(task["id"])["status"] == "rejected"


def test_reviewed_by_alone_cannot_self_certify(
    monkeypatch, capsys, tmp_path, config_file,
):
    _write_artifact("capital-flow")
    task = _enqueue_anomaly(config_file)
    _, order = _run_cli(monkeypatch, capsys, "next", "--worker", "openclaw")
    finding = {
        "schema": "research_finding_v1",
        "task_id": task["id"],
        "role": "risk_redteam",
        "stance": "oppose",
        "confidence": 0.8,
        "summary": "资金面证据与候选逻辑冲突。",
        "evidence_refs": ["fact_artifacts.capital-flow"],
        "risk_flags": ["capital_outflow"],
    }
    finding_path = tmp_path / "finding.json"
    finding_path.write_text(json.dumps(finding), encoding="utf-8")

    code, result = _run_cli(
        monkeypatch, capsys,
        "submit", "--task", task["id"], "--role", "risk_redteam",
        "--file", str(finding_path), "--worker", "openclaw",
        "--claim-id", order["claim_id"], "--model", "fixture-model",
        "--reviewed-by", "arbitrary-name",
    )

    assert code == 0
    assert result["synthesis"]["synthesis"]["verdict"] == "review_only"


def test_mismatched_approval_fails_closed_before_submission(
    monkeypatch, capsys, tmp_path, config_file,
):
    _write_artifact("capital-flow")
    task = _enqueue_anomaly(config_file)
    _, order = _run_cli(monkeypatch, capsys, "next", "--worker", "openclaw")
    finding = {
        "schema": "research_finding_v1",
        "task_id": task["id"],
        "role": "risk_redteam",
        "stance": "oppose",
        "confidence": 0.8,
        "summary": "资金面证据与候选逻辑冲突。",
        "evidence_refs": ["fact_artifacts.capital-flow"],
        "risk_flags": ["capital_outflow"],
    }
    finding_path = tmp_path / "finding.json"
    finding_path.write_text(json.dumps(finding), encoding="utf-8")
    approval_path = _trusted_approval_path(tmp_path)
    approval_path.write_text(json.dumps({
        "schema": "research_finding_approval_v1",
        "task_id": task["id"],
        "role": "risk_redteam",
        "claim_id": "stale-claim",
        "finding_sha256": runner.agent_evidence.finding_sha256(finding),
        "status": "approved",
        "reviewer": "risk-owner",
        "approved_at": "2026-07-02T16:00:00+00:00",
    }), encoding="utf-8")

    code, result = _run_cli(
        monkeypatch, capsys,
        "submit", "--task", task["id"], "--role", "risk_redteam",
        "--file", str(finding_path), "--worker", "openclaw",
        "--claim-id", order["claim_id"], "--model", "fixture-model",
        "--approval-file", str(approval_path),
    )

    assert code == 2
    assert "approval_claim_mismatch" in result["errors"]
    assert bus.find_task(task["id"])["roles"]["risk_redteam"]["status"] == "claimed"


def test_approval_outside_trusted_root_or_via_symlink_is_rejected(
    monkeypatch, capsys, tmp_path, config_file,
):
    _write_artifact("capital-flow")
    task = _enqueue_anomaly(config_file)
    _, order = _run_cli(monkeypatch, capsys, "next", "--worker", "openclaw")
    finding = {
        "schema": "research_finding_v1",
        "task_id": task["id"],
        "role": "risk_redteam",
        "stance": "oppose",
        "confidence": 0.8,
        "summary": "资金面证据与候选逻辑冲突。",
        "evidence_refs": ["fact_artifacts.capital-flow"],
        "risk_flags": ["capital_outflow"],
    }
    finding_path = tmp_path / "finding.json"
    finding_path.write_text(json.dumps(finding), encoding="utf-8")
    untrusted = tmp_path / "untrusted-approval.json"
    untrusted.write_text(json.dumps({
        "schema": "research_finding_approval_v1",
        "task_id": task["id"],
        "role": "risk_redteam",
        "claim_id": order["claim_id"],
        "finding_sha256": runner.agent_evidence.finding_sha256(finding),
        "status": "approved",
        "reviewer": "risk-owner",
        "approved_at": "2026-07-02T16:00:00+00:00",
    }), encoding="utf-8")

    code, result = _run_cli(
        monkeypatch, capsys,
        "submit", "--task", task["id"], "--role", "risk_redteam",
        "--file", str(finding_path), "--worker", "openclaw",
        "--claim-id", order["claim_id"], "--model", "fixture-model",
        "--approval-file", str(untrusted),
    )
    assert code == 2
    assert "approval_path_untrusted" in result["errors"][0]

    symlink = _trusted_approval_path(tmp_path, "approval-link.json")
    symlink.symlink_to(untrusted)
    code, result = _run_cli(
        monkeypatch, capsys,
        "submit", "--task", task["id"], "--role", "risk_redteam",
        "--file", str(finding_path), "--worker", "openclaw",
        "--claim-id", order["claim_id"], "--model", "fixture-model",
        "--approval-file", str(symlink),
    )
    assert code == 2
    assert (
        "approval_path_untrusted" in result["errors"][0]
        or "approval_path_symlink_rejected" in result["errors"][0]
    )


def test_unreviewed_model_finding_is_review_only():
    finding = {
        "stance": "support",
        "confidence": 0.9,
        "model_run_manifest": {"execution_eligible": False},
    }
    decision = runner.research_synthesis.decide_verdict(
        {"thesis_builder": finding},
        {"advance_min_support_confidence": 0.6},
    )
    assert decision["verdict"] == "review_only"
    assert decision["basis"] == "human_review_required:thesis_builder"


def test_insufficient_pack_auto_abstains_without_work_order(
    monkeypatch, capsys, config_file,
):
    task = _enqueue_anomaly(config_file)
    code, output = _run_cli(monkeypatch, capsys, "next", "--worker", "hermes")
    assert code == 0
    assert output["status"] == "abstained_insufficient_evidence"
    refreshed = bus.find_task(task["id"])
    assert refreshed["status"] == "abstained"
    assert refreshed["verdict"] == "abstained"


def test_submit_rejects_contract_violations(
    monkeypatch, capsys, tmp_path, config_file,
):
    _write_artifact("capital-flow")
    task = _enqueue_anomaly(config_file)
    _, order = _run_cli(monkeypatch, capsys, "next", "--worker", "openclaw")

    bad = {
        "schema": "research_finding_v1",
        "task_id": task["id"],
        "role": "risk_redteam",
        "stance": "support",
        "confidence": 0.9,
        "summary": "缺反证的看多结论",
        "evidence_refs": ["fact_artifacts.capital-flow"],
    }
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    code, result = _run_cli(
        monkeypatch, capsys,
        "submit", "--task", task["id"], "--role", "risk_redteam",
        "--file", str(bad_path), "--claim-id", order["claim_id"],
    )
    assert code == 2
    assert result["ok"] is False
    assert any("counterevidence" in error for error in result["errors"])
    assert bus.find_task(task["id"])["status"] == "in_progress"


def test_abstain_command_completes_role(monkeypatch, capsys, config_file):
    _write_artifact("capital-flow")
    task = _enqueue_anomaly(config_file)
    _, order = _run_cli(monkeypatch, capsys, "next", "--worker", "openclaw")

    code, result = _run_cli(
        monkeypatch, capsys,
        "abstain", "--task", task["id"], "--role", "risk_redteam",
        "--reason", "证据包缺少盘口数据", "--claim-id", order["claim_id"],
    )
    assert code == 0
    assert result["ok"] is True
    assert result["synthesis"]["synthesis"]["verdict"] == "abstained"


def test_status_and_fail_paths(monkeypatch, capsys, config_file):
    _write_artifact("capital-flow")
    task = _enqueue_anomaly(config_file)
    _, order = _run_cli(monkeypatch, capsys, "next", "--worker", "openclaw")

    code, result = _run_cli(
        monkeypatch, capsys,
        "fail", "--task", task["id"], "--role", "risk_redteam",
        "--error", "model timeout", "--worker", "openclaw",
        "--claim-id", order["claim_id"],
    )
    assert code == 0
    assert result["role_status"] == "pending"

    code, summary = _run_cli(monkeypatch, capsys, "status")
    assert code == 0
    assert summary["by_status"]["in_progress"] == 1
    assert summary["active"][0]["roles"]["risk_redteam"] == "pending"
