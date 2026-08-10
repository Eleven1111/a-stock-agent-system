import copy
import json
from pathlib import Path

import pytest

import dataset_contract
import dual_agent_compiler as compiler


ROOT = Path(__file__).resolve().parents[1]
CATALOG = dataset_contract.load_catalog(ROOT / "config" / "dataset_catalog.json")
CONTRACT = dataset_contract.resolve_dataset(
    CATALOG, "cross_sectional_direction_rows_v1"
)
PACK = {
    "schema": "research_evidence_pack_v1",
    "ref": "pack-fixture",
    "payload": {
        "fact_artifacts": [{"job_id": "candidate-discovery"}],
        "quality": {"status": "ok"},
    },
}


def _proposal():
    return {
        "schema": "research_proposal_v1",
        "task_id": "task-1",
        "kind": "direction_diagnostic",
        "subject": {"code": "600000", "name": "浦发银行"},
        "trading_date": "2026-08-10",
        "created_at": "2026-08-10T09:30:00+08:00",
        "verdict": "advance",
        "synthesis_ref": "research-committee/boards/task-1/synthesis.json",
        "synthesis_sha256": "a" * 64,
        "summary": ["需要验证截面分数方向。"],
        "counterevidence": [],
        "invalidation_conditions": [],
        "policy_gate_required": True,
        "live_effect": "none_until_strategy_registry_and_decision_policy_pass",
    }


def _plan():
    return {
        "schema": "analysis_plan_v1",
        "plan_id": "direction-diagnostic",
        "question": "截面分数方向是否有效？",
        "research_only": True,
        "inputs": {
            "direction_rows": {
                "kind": "dataset",
                "dataset_id": CONTRACT["dataset_id"],
                "contract_hash": CONTRACT["contract_hash"],
                "catalog_hash": CATALOG["catalog_hash"],
                "coverage_ratio": 1.0,
            }
        },
        "nodes": [
            {
                "id": "cohorts",
                "operator": "group_direction_cohorts_v1",
                "inputs": ["direction_rows"],
                "params": {},
            },
            {
                "id": "direction",
                "operator": "cross_sectional_direction_v1",
                "inputs": ["cohorts"],
                "params": {},
            },
        ],
        "outputs": ["direction"],
    }


def _draft(request, **overrides):
    value = {
        "schema": "analysis_plan_draft_v1",
        "task_id": "task-1",
        "role": "analysis_plan_author",
        "request_hash": request["request_hash"],
        "plan": _plan(),
        "evidence_refs": ["fact_artifacts.candidate-discovery"],
        "confidence": 0.8,
        "summary": "使用白名单方向算子验证现有分数。",
    }
    value.update(overrides)
    return value


def _payload(request, **overrides):
    value = {
        "status": "completed",
        "finding": _draft(request),
        "tool_usage_summary": {
            "tools": ["read_evidence_pack", "read_dataset_catalog"],
            "state_writes": [],
        },
        "model_usage": {"input_tokens": 100, "output_tokens": 50},
    }
    value.update(overrides)
    return value


def _request():
    return compiler.build_compile_request(
        _proposal(),
        question="截面分数方向是否有效？",
        evidence_pack_ref="pack-fixture",
        catalog=CATALOG,
    )


def _reseal_request(request):
    body = {key: value for key, value in request.items() if key != "request_hash"}
    return {**body, "request_hash": compiler._hash(body)}


def test_request_binds_existing_research_agent_output_and_allowlists():
    request = _request()

    assert request["schema"] == "research_compile_request_v1"
    assert request["interaction_agent"] == "existing_research_committee"
    assert request["plan_agent_role"] == "analysis_plan_author"
    assert request["research_proposal_hash"].startswith("sha256:")
    assert request["catalog_hash"] == CATALOG["catalog_hash"]
    assert "python_eval" not in request["allowed_operators"]
    assert request["request_hash"].startswith("sha256:")


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"schema": "wrong"}, "research_proposal_schema_invalid"),
        ({"task_id": ""}, "research_proposal_task_id_missing"),
        ({"policy_gate_required": False}, "research_proposal_policy_gate_missing"),
        ({"synthesis_ref": ""}, "research_proposal_synthesis_ref_missing"),
        ({"synthesis_sha256": "bad"}, "research_proposal_synthesis_hash_invalid"),
        ({"live_effect": "may_trade"}, "research_proposal_live_effect_invalid"),
    ],
)
def test_compile_request_rejects_untrusted_interaction_artifacts(mutation, expected):
    proposal = {**_proposal(), **mutation}

    with pytest.raises(compiler.DualAgentCompilerError, match=expected):
        compiler.build_compile_request(
            proposal,
            question="截面分数方向是否有效？",
            evidence_pack_ref="pack-fixture",
            catalog=CATALOG,
        )


@pytest.mark.parametrize("runtime", ["hermes", "openclaw", "fake"])
def test_existing_runtime_adapters_compile_the_same_bounded_plan(runtime):
    request = _request()
    seen = []

    def turn(agent_request, evidence_pack):
        seen.append((agent_request, evidence_pack))
        return _payload(request)

    result = compiler.run_compile_chain(
        request,
        catalog=CATALOG,
        runtime=runtime,
        model="test-model-v1",
        turn=turn,
        evidence_pack=PACK,
        now="2026-08-10T10:00:00+08:00",
    )

    assert result["status"] == "compiled"
    assert result["handoff_status"] == "ready_for_deterministic_execution"
    assert result["research_only"] is True
    assert result["trading_action"] == "none"
    assert result["sealed_plan"]["plan_hash"].startswith("sha256:")
    assert seen[0][0].role == "analysis_plan_author"
    assert "execute_python" not in seen[0][0].allowed_tools
    assert (
        seen[0][0].model_metadata["compile_request"]["request_hash"]
        == request["request_hash"]
    )
    assert seen[0][1] == PACK


def test_tampered_request_is_blocked_before_agent_invocation():
    request = {**_request(), "question": "tampered"}
    called = []

    result = compiler.run_compile_chain(
        request,
        catalog=CATALOG,
        runtime="fake",
        model="test-model-v1",
        turn=lambda *_: called.append(True),
        evidence_pack=PACK,
        now="2026-08-10T10:00:00+08:00",
    )

    assert result["status"] == "blocked"
    assert "compile_request_hash_mismatch" in result["reason_codes"]
    assert called == []


def test_unknown_request_fields_and_naive_compile_time_fail_closed():
    request = {**_request(), "shell_command": "python payload.py"}
    result = compiler.run_compile_chain(
        request,
        catalog=CATALOG,
        runtime="fake",
        model="test-model-v1",
        turn=lambda *_: None,
        evidence_pack=PACK,
        now="2026-08-10T10:00:00+08:00",
    )
    assert "compile_request_field_not_allowed:shell_command" in result["reason_codes"]

    with pytest.raises(compiler.DualAgentCompilerError, match="timezone_required"):
        compiler.run_compile_chain(
            _request(),
            catalog=CATALOG,
            runtime="fake",
            model="test-model-v1",
            turn=lambda *_: None,
            evidence_pack=PACK,
            now="2026-08-10T10:00:00",
        )


@pytest.mark.parametrize(
    ("tool_usage", "expected"),
    [
        ({"tools": ["execute_python"], "state_writes": []}, "tool_not_allowed"),
        (
            {"tools": ["read_evidence_pack"], "state_writes": ["signal_ledger.jsonl"]},
            "forbidden_state_write",
        ),
    ],
)
def test_plan_agent_cannot_execute_code_or_write_fact_state(tool_usage, expected):
    request = _request()
    payload = _payload(request, tool_usage_summary=tool_usage)

    result = compiler.run_compile_chain(
        request,
        catalog=CATALOG,
        runtime="fake",
        model="test-model-v1",
        turn=lambda *_: payload,
        evidence_pack=PACK,
        now="2026-08-10T10:00:00+08:00",
    )

    assert result["status"] == "blocked"
    assert expected in result["reason_codes"]
    assert "sealed_plan" not in result


def test_arbitrary_operator_is_rejected_by_deterministic_compiler():
    request = _request()
    plan = _plan()
    plan["nodes"][0]["operator"] = "python_eval"
    payload = _payload(request, finding=_draft(request, plan=plan))

    result = compiler.run_compile_chain(
        request,
        catalog=CATALOG,
        runtime="fake",
        model="test-model-v1",
        turn=lambda *_: payload,
        evidence_pack=PACK,
        now="2026-08-10T10:00:00+08:00",
    )

    assert result["status"] == "blocked"
    assert "operator_not_allowed:python_eval" in result["reason_codes"]


def test_draft_must_bind_request_and_resolve_evidence_refs():
    request = _request()
    bad_hash = _payload(request, finding=_draft(request, request_hash="sha256:" + "0" * 64))
    unresolved = _payload(
        request,
        finding=_draft(request, evidence_refs=["fact_artifacts.missing"]),
    )

    first = compiler.run_compile_chain(
        request,
        catalog=CATALOG,
        runtime="fake",
        model="test-model-v1",
        turn=lambda *_: bad_hash,
        evidence_pack=PACK,
        now="2026-08-10T10:00:00+08:00",
    )
    second = compiler.run_compile_chain(
        request,
        catalog=CATALOG,
        runtime="fake",
        model="test-model-v1",
        turn=lambda *_: unresolved,
        evidence_pack=PACK,
        now="2026-08-10T10:00:00+08:00",
    )

    assert "draft_request_hash_mismatch" in first["reason_codes"]
    assert "evidence_ref_unresolved" in second["reason_codes"]


@pytest.mark.parametrize(
    ("draft_change", "expected"),
    [
        ({"schema": "wrong"}, "schema_mismatch"),
        ({"task_id": "other"}, "draft_task_id_mismatch"),
        ({"role": "code_executor"}, "draft_role_invalid"),
        ({"unexpected": True}, "plan_draft_field_not_allowed:unexpected"),
    ],
)
def test_draft_envelope_is_strict(draft_change, expected):
    request = _request()
    payload = _payload(request, finding=_draft(request, **draft_change))

    result = compiler.run_compile_chain(
        request,
        catalog=CATALOG,
        runtime="fake",
        model="test-model-v1",
        turn=lambda *_: payload,
        evidence_pack=PACK,
        now="2026-08-10T10:00:00+08:00",
    )

    assert expected in result["reason_codes"]


def test_plan_question_cannot_drift_from_interaction_request():
    request = _request()
    plan = _plan()
    plan["question"] = "另一个问题"

    result = compiler.run_compile_chain(
        request,
        catalog=CATALOG,
        runtime="fake",
        model="test-model-v1",
        turn=lambda *_: _payload(request, finding=_draft(request, plan=plan)),
        evidence_pack=PACK,
        now="2026-08-10T10:00:00+08:00",
    )

    assert result["reason_codes"] == ["plan_question_mismatch"]


def test_catalog_and_evidence_pack_identity_fail_closed():
    request = _request()
    changed_catalog = copy.deepcopy(CATALOG)
    changed_catalog["catalog_hash"] = "sha256:" + "0" * 64
    wrong_pack = {**PACK, "ref": "different-pack"}

    catalog_result = compiler.run_compile_chain(
        request,
        catalog=changed_catalog,
        runtime="fake",
        model="test-model-v1",
        turn=lambda *_: _payload(request),
        evidence_pack=PACK,
        now="2026-08-10T10:00:00+08:00",
    )
    pack_result = compiler.run_compile_chain(
        request,
        catalog=CATALOG,
        runtime="fake",
        model="test-model-v1",
        turn=lambda *_: _payload(request),
        evidence_pack=wrong_pack,
        now="2026-08-10T10:00:00+08:00",
    )

    assert "catalog_hash_mismatch" in catalog_result["reason_codes"]
    assert "evidence_pack_ref_mismatch" in pack_result["reason_codes"]


def test_missing_model_invalid_pack_and_unknown_runtime_are_blocked():
    request = _request()
    common = {
        "catalog": CATALOG,
        "turn": lambda *_: _payload(request),
        "now": "2026-08-10T10:00:00+08:00",
    }

    no_model = compiler.run_compile_chain(
        request, runtime="fake", model="", evidence_pack=PACK, **common
    )
    invalid_pack = compiler.run_compile_chain(
        request, runtime="fake", model="test-model-v1", evidence_pack=[], **common
    )
    unknown_runtime = compiler.run_compile_chain(
        request,
        runtime="new-runtime",
        model="test-model-v1",
        evidence_pack=PACK,
        **common,
    )

    assert "model_version_unconfigured" in no_model["reason_codes"]
    assert "evidence_pack_invalid" in invalid_pack["reason_codes"]
    assert "unknown agent runtime" in unknown_runtime["reason_codes"][0]


def test_same_inputs_and_frozen_time_compile_identically():
    request = _request()
    kwargs = {
        "catalog": CATALOG,
        "runtime": "fake",
        "model": "test-model-v1",
        "turn": lambda *_: _payload(request),
        "evidence_pack": PACK,
        "now": "2026-08-10T10:00:00+08:00",
    }

    first = compiler.run_compile_chain(request, **kwargs)
    second = compiler.run_compile_chain(request, **kwargs)

    assert first == second
    assert first["compilation_hash"].startswith("sha256:")


def test_noncanonical_agent_telemetry_is_blocked_instead_of_crashing():
    request = _request()
    payload = _payload(request, model_usage={"cost": float("nan")})

    result = compiler.run_compile_chain(
        request,
        catalog=CATALOG,
        runtime="fake",
        model="test-model-v1",
        turn=lambda *_: payload,
        evidence_pack=PACK,
        now="2026-08-10T10:00:00+08:00",
    )

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["agent_result_not_canonical"]


def test_tampered_persisted_handoff_is_rejected(tmp_path):
    request = _request()
    result = compiler.run_compile_chain(
        request,
        catalog=CATALOG,
        runtime="fake",
        model="test-model-v1",
        turn=lambda *_: _payload(request),
        evidence_pack=PACK,
        now="2026-08-10T10:00:00+08:00",
    )
    stored = compiler.store_compilation(result, store_dir=str(tmp_path))
    duplicate = compiler.store_compilation(result, store_dir=str(tmp_path))
    assert stored["created"] is True
    assert duplicate["created"] is False
    assert compiler.load_compilation(
        result["compilation_hash"], store_dir=str(tmp_path)
    ) == result

    path = Path(stored["artifact_path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["handoff_status"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(compiler.DualAgentCompilerError, match="compilation_hash_mismatch"):
        compiler.load_compilation(result["compilation_hash"], store_dir=str(tmp_path))
