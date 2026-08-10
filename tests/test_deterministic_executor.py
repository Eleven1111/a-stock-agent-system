import copy
import json
import subprocess
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import dataset_contract
import deterministic_executor as executor
import dual_agent_compiler as compiler


ROOT = Path(__file__).resolve().parents[1]
CATALOG = dataset_contract.load_catalog(ROOT / "config" / "dataset_catalog.json")
CONTRACT = dataset_contract.resolve_dataset(
    CATALOG, "cross_sectional_direction_rows_v1"
)
NOW = "2026-08-10T10:00:00+08:00"
PACK = {
    "schema": "research_evidence_pack_v1",
    "ref": "pack-fixture",
    "payload": {
        "fact_artifacts": [{"job_id": "candidate-discovery"}],
        "quality": {"status": "ok"},
    },
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


def _compilation():
    request = compiler.build_compile_request(
        _proposal(),
        question=_plan()["question"],
        evidence_pack_ref=PACK["ref"],
        catalog=CATALOG,
    )
    draft = {
        "schema": "analysis_plan_draft_v1",
        "task_id": "task-1",
        "role": "analysis_plan_author",
        "request_hash": request["request_hash"],
        "plan": _plan(),
        "evidence_refs": ["fact_artifacts.candidate-discovery"],
        "confidence": 0.8,
        "summary": "使用白名单方向算子验证现有分数。",
    }
    payload = {
        "status": "completed",
        "finding": draft,
        "tool_usage_summary": {
            "tools": ["read_evidence_pack", "read_dataset_catalog"],
            "state_writes": [],
        },
        "model_usage": {},
    }
    return compiler.run_compile_chain(
        request,
        catalog=CATALOG,
        runtime="fake",
        model="test-model-v1",
        turn=lambda *_: payload,
        evidence_pack=PACK,
        now=NOW,
    )


def _inputs():
    rows = []
    start = date(2026, 6, 1)
    for cohort_index in range(5):
        src = start + timedelta(days=cohort_index * 8)
        dst = src + timedelta(days=7)
        for index in range(100):
            rows.append(
                {
                    "entity_id": f"{index + 1:06d}",
                    "src": src.isoformat(),
                    "dst": dst.isoformat(),
                    "score": float(100 - index),
                    "forward_return": float(index) / 1000,
                    "score_available_at": f"{src.isoformat()}T15:00:00+08:00",
                    "outcome_available_at": f"{dst.isoformat()}T15:01:00+08:00",
                    "snapshot_ref": "sha256:" + str(cohort_index) * 64,
                }
            )
    return {"direction_rows": rows}


def _rehash_compilation(value):
    body = {key: item for key, item in value.items() if key != "compilation_hash"}
    return compiler._artifact(body)


def test_executes_twice_in_isolation_and_emits_bound_validation(tmp_path):
    result = executor.execute_compilation(
        _compilation(),
        _inputs(),
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
    )

    assert result["status"] == "validated"
    assert result["research_only"] is True
    assert result["trading_action"] == "none"
    assert result["validation"]["status"] == "passed"
    assert result["validation"]["replay_count"] == 2
    assert result["validation"]["replay_deterministic"] is True
    assert result["run"]["outputs"]["direction"]["verdict"] == "direction_inverted"
    assert result["run"]["result_hash"].startswith("sha256:")
    assert result["execution_hash"].startswith("sha256:")
    assert list(tmp_path.iterdir()) == []


def test_frozen_inputs_produce_identical_execution_evidence(tmp_path):
    kwargs = {
        "catalog": CATALOG,
        "validated_at": NOW,
        "workspace_root": str(tmp_path),
    }

    first = executor.execute_compilation(_compilation(), _inputs(), **kwargs)
    second = executor.execute_compilation(_compilation(), _inputs(), **kwargs)

    assert first == second


def test_tampered_compilation_is_rejected_before_subprocess(monkeypatch, tmp_path):
    compilation = _compilation()
    compilation["sealed_plan"]["question"] = "tampered"
    called = []
    monkeypatch.setattr(
        executor.subprocess,
        "run",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    result = executor.execute_compilation(
        compilation,
        _inputs(),
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
    )

    assert result["status"] == "blocked"
    assert "compilation_hash_mismatch" in result["reason_codes"]
    assert called == []


def test_blocked_handoff_and_rehashed_arbitrary_operator_never_execute(tmp_path):
    blocked = _compilation()
    blocked["status"] = "blocked"
    blocked["handoff_status"] = "not_created"
    blocked = _rehash_compilation(blocked)

    invalid_plan = _compilation()
    invalid_plan["sealed_plan"]["nodes"][0]["operator"] = "python_eval"
    invalid_plan["sealed_plan"].pop("plan_hash")
    invalid_plan = _rehash_compilation(invalid_plan)

    first = executor.execute_compilation(
        blocked,
        _inputs(),
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
    )
    second = executor.execute_compilation(
        invalid_plan,
        _inputs(),
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
    )

    assert "handoff_not_ready" in first["reason_codes"]
    assert "operator_not_allowed:python_eval" in second["reason_codes"]


def test_missing_or_invalid_inputs_are_reported_as_validation_failures(tmp_path):
    missing = executor.execute_compilation(
        _compilation(),
        {},
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
    )
    invalid = executor.execute_compilation(
        _compilation(),
        {"direction_rows": [{"entity_id": "600000"}]},
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
    )

    assert missing["status"] == "blocked"
    assert "execution_input_missing:direction_rows" in missing["reason_codes"]
    assert invalid["status"] == "blocked"
    assert "required_field:src" in invalid["reason_codes"]


def test_timeout_and_non_json_child_output_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        executor.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=args[0], timeout=1)
        ),
    )
    timeout_result = executor.execute_compilation(
        _compilation(),
        _inputs(),
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
        timeout_seconds=1,
    )
    assert timeout_result["reason_codes"] == ["executor_timeout"]

    monkeypatch.setattr(
        executor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="not-json", stderr=""
        ),
    )
    invalid_result = executor.execute_compilation(
        _compilation(),
        _inputs(),
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
    )
    assert invalid_result["reason_codes"] == ["executor_output_invalid"]


def test_executor_configuration_and_noncanonical_inputs_fail_closed(tmp_path):
    with pytest.raises(executor.DeterministicExecutorError, match="timeout_seconds_invalid"):
        executor.execute_compilation(
            _compilation(),
            _inputs(),
            catalog=CATALOG,
            validated_at=NOW,
            workspace_root=str(tmp_path),
            timeout_seconds=0,
        )
    with pytest.raises(executor.DeterministicExecutorError, match="timezone_required"):
        executor.execute_compilation(
            _compilation(),
            _inputs(),
            catalog=CATALOG,
            validated_at="2026-08-10T10:00:00",
        )

    result = executor.execute_compilation(
        _compilation(),
        {"direction_rows": [{"score": float("nan")}]},
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
    )
    assert result["reason_codes"] == ["payload_not_canonical_json"]


def test_second_run_failure_and_replay_difference_are_blocking(monkeypatch, tmp_path):
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            return {"result_hash": "sha256:first"}, []
        return None, ["synthetic_second_failure"]

    monkeypatch.setattr(executor, "_run_once", fail_second)
    failed = executor.execute_compilation(
        _compilation(),
        _inputs(),
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
    )
    assert failed["reason_codes"] == ["synthetic_second_failure"]

    values = iter(
        [
            ({"result_hash": "sha256:first"}, []),
            ({"result_hash": "sha256:second"}, []),
        ]
    )
    monkeypatch.setattr(executor, "_run_once", lambda *args, **kwargs: next(values))
    different = executor.execute_compilation(
        _compilation(),
        _inputs(),
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
    )
    assert different["reason_codes"] == ["replay_nondeterministic"]


def test_executor_uses_fixed_argv_isolated_state_and_no_parent_secrets(
    monkeypatch, tmp_path
):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(
            returncode=2,
            stdout=json.dumps(
                {
                    "schema": "analysis_run_v1",
                    "status": "blocked",
                    "research_only": True,
                    "trading_action": "none",
                    "errors": ["synthetic_child_block"],
                }
            ),
            stderr="ignored secret",
        )

    monkeypatch.setenv("TOP_SECRET_TOKEN", "must-not-propagate")
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    result = executor.execute_compilation(
        _compilation(),
        _inputs(),
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
    )

    argv, kwargs = seen[0]
    assert argv[1].endswith("scripts/run_analysis_plan.py")
    assert "shell" not in kwargs or kwargs["shell"] is False
    assert kwargs["cwd"].startswith(str(tmp_path))
    assert kwargs["env"]["A_STOCK_STATE_HOME"].startswith(str(tmp_path))
    assert "TOP_SECRET_TOKEN" not in kwargs["env"]
    assert "ignored secret" not in json.dumps(result)
    assert result["reason_codes"] == ["synthetic_child_block"]


def test_execution_artifact_store_is_idempotent_and_tamper_evident(tmp_path):
    result = executor.execute_compilation(
        _compilation(),
        _inputs(),
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path / "work"),
    )
    store = tmp_path / "store"
    first = executor.store_execution(result, store_dir=str(store))
    second = executor.store_execution(result, store_dir=str(store))
    assert first["created"] is True
    assert second["created"] is False
    assert executor.load_execution(
        result["execution_hash"], store_dir=str(store)
    ) == result

    path = Path(first["artifact_path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(executor.DeterministicExecutorError, match="execution_hash_mismatch"):
        executor.load_execution(result["execution_hash"], store_dir=str(store))


def test_nested_validation_cannot_be_forged_with_a_rehashed_outer_artifact(tmp_path):
    result = executor.execute_compilation(
        _compilation(),
        _inputs(),
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
    )
    forged = dict(result)
    forged["validation"] = {**result["validation"], "replay_deterministic": False}
    body = {key: item for key, item in forged.items() if key != "execution_hash"}
    forged["execution_hash"] = executor._hash(body)

    with pytest.raises(executor.DeterministicExecutorError, match="validation_hash_mismatch"):
        executor.store_execution(forged, store_dir=str(tmp_path / "store"))


def test_rehashing_outer_and_validation_cannot_hide_tampered_run(tmp_path):
    result = executor.execute_compilation(
        _compilation(),
        _inputs(),
        catalog=CATALOG,
        validated_at=NOW,
        workspace_root=str(tmp_path),
    )
    forged = dict(result)
    forged["run"] = copy_run = copy.deepcopy(result["run"])
    copy_run["outputs"]["direction"]["verdict"] = "direction_confirmed"
    validation = dict(result["validation"])
    validation_body = {
        key: item for key, item in validation.items() if key != "validation_hash"
    }
    validation["validation_hash"] = executor._hash(validation_body)
    forged["validation"] = validation
    body = {key: item for key, item in forged.items() if key != "execution_hash"}
    forged["execution_hash"] = executor._hash(body)

    with pytest.raises(executor.DeterministicExecutorError, match="execution_run_hash_mismatch"):
        executor.store_execution(forged, store_dir=str(tmp_path / "store"))
