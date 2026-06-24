"""Regression coverage for the token-bounded model-facing surfaces.

These lock in the behaviour added by the token-optimization pass: the runtime
context loads a lite projection by default, and artifacts cap the raw stdout
blob while keeping the derived summary intact.
"""

import json

from runtime_context import build_artifact
from scripts.agent_runtime_context import build_runtime_context
from scripts.agent_state_projector import (
    project_monitor_lite,
    project_recommendation_lite,
    project_state_lite,
)


HEAVY_REC = {
    "id": "open-2026-06-22-002436",
    "code": "002436",
    "name": "兴森科技",
    "date": "2026-06-22",
    "action": "avoid",
    "grade": "B",
    "confidence": "medium",
    "entry_price": 53.69,
    "price_range": "53.42-53.96",
    "target_price": 59.91,
    "stop_price": 49.96,
    "horizon": "T+1",
    "rationale": "符合趋势策略观察窗口",
    "strategy_id": "daban:first_board_reseal",
    "outcome": "pending",
    "settleable_signal": False,
    "risks": ["r1", "r2", "r3", "r4", "r5"],
    "position_sizing": {"recommended_position_pct": 0.0, "gating_status": "unverified"},
    "execution_constraints": {"earliest_sell_date": "2026-06-23", "same_day_sell_allowed": False},
    "quality_report": {"x": "y" * 1000},
    "policy_decision": {"x": "y" * 1000},
    "research_evidence": {"x": "y" * 800},
    "correlation_id": "corr-xxx",
}

HEAVY_MONITOR = {
    "id": "stock:002297",
    "kind": "stock",
    "key": "002297",
    "created_at": "2026-06-22T09:29:11",
    "label": "博云新材",
    "status": "active",
    "source": "candidate_discovery",
    "source_group": "daily_observation",
    "updated_at": "2026-06-22T09:29:11",
    "metadata": {"candidate_rank": 1, "blob": "z" * 500},
}


def test_recommendation_lite_keeps_decision_fields_drops_audit_blocks():
    lite = project_recommendation_lite(HEAVY_REC)

    # Explanation-critical fields survive.
    for key in ("code", "name", "action", "grade", "entry_price", "stop_price", "horizon"):
        assert lite[key] == HEAVY_REC[key]
    assert lite["top_risks"] == ["r1", "r2", "r3"]  # capped at 3
    assert lite["position_pct"] == 0.0
    assert lite["position_status"] == "unverified"
    assert lite["earliest_sell_date"] == "2026-06-23"

    # Heavy audit/provenance blocks are dropped from the model surface.
    for dropped in ("quality_report", "policy_decision", "research_evidence", "correlation_id"):
        assert dropped not in lite

    # And the projection is materially smaller.
    full_size = len(json.dumps(HEAVY_REC, ensure_ascii=False))
    lite_size = len(json.dumps(lite, ensure_ascii=False))
    assert lite_size < full_size * 0.5


def test_monitor_lite_keeps_only_identity_fields():
    lite = project_monitor_lite(HEAVY_MONITOR)
    assert lite == {
        "key": "002297",
        "label": "博云新材",
        "kind": "stock",
        "status": "active",
        "source_group": "daily_observation",
    }
    assert "metadata" not in lite
    assert "created_at" not in lite


def test_project_state_lite_preserves_non_list_fields():
    state = {
        "schema": "a_stock_agent_state_v1",
        "portfolio": {"cash": 1, "positions": []},
        "recommendations": [HEAVY_REC],
        "monitors": [HEAVY_MONITOR],
        "behavior_risk": {"schema": "behavior_risk_v1"},
    }
    lite = project_state_lite(state)
    assert lite["schema"] == state["schema"]
    assert lite["portfolio"] == state["portfolio"]
    assert lite["behavior_risk"] == state["behavior_risk"]
    assert "quality_report" not in lite["recommendations"][0]
    assert "metadata" not in lite["monitors"][0]


def test_runtime_context_defaults_to_lite_view(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    context = build_runtime_context(refresh=True)
    assert context["view"] == "lite"
    # Contract keys the loader test depends on are still present.
    assert context["runtime_contract"]["source_of_truth"] == "signal_ledger.jsonl"
    assert context["behavior_risk"]["schema"] == "behavior_risk_v1"


def test_runtime_context_full_view_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    context = build_runtime_context(refresh=True, lite=False)
    assert context["view"] == "full"


def test_build_artifact_bounds_stdout_but_keeps_summary(monkeypatch):
    monkeypatch.setenv("A_STOCK_MAX_ARTIFACT_STDOUT", "500")
    payload = {"schema": "auction_finalize_v2", "factors": list(range(200))}
    big_stdout = json.dumps(payload, ensure_ascii=False)
    assert len(big_stdout) > 500

    artifact = build_artifact(
        job={"id": "auction-finalize"},
        run_id="t1",
        command="x",
        cwd=".",
        returncode=0,
        stdout=big_stdout,
        stderr="",
        started_at="2026-06-22T09:30:05+08:00",
        finished_at="2026-06-22T09:30:06+08:00",
        duration_seconds=1.2,
        context_artifacts=[],
    )

    assert len(artifact["stdout"]) == 500
    assert artifact["stdout_truncated_chars"] == len(big_stdout) - 500
    # Summary/has_signal are still derived from the untruncated stdout.
    assert artifact["summary"]["schema"] == "auction_finalize_v2"
    assert artifact["summary"]["factors_count"] == 200
    assert artifact["has_signal"] is True


def test_build_artifact_keeps_small_stdout_whole(monkeypatch):
    monkeypatch.delenv("A_STOCK_MAX_ARTIFACT_STDOUT", raising=False)
    stdout = json.dumps({"status": "ok", "alerts": [{"x": 1}]}, ensure_ascii=False)
    artifact = build_artifact(
        job={"id": "demo"},
        run_id="t2",
        command="x",
        cwd=".",
        returncode=0,
        stdout=stdout,
        stderr="",
        started_at="2026-06-22T09:30:05+08:00",
        finished_at="2026-06-22T09:30:06+08:00",
        duration_seconds=0.1,
        context_artifacts=[],
    )
    assert artifact["stdout"] == stdout
    assert artifact["stdout_truncated_chars"] == 0
