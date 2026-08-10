import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import analysis_plan
import dataset_contract
from scripts import run_analysis_plan


ROOT = Path(__file__).resolve().parents[1]
CATALOG = dataset_contract.load_catalog(ROOT / "config" / "dataset_catalog.json")
CONTRACT = dataset_contract.resolve_dataset(
    CATALOG, "cross_sectional_direction_rows_v1"
)


def _plan():
    return {
        "schema": "analysis_plan_v1",
        "plan_id": "direction-and-recall",
        "question": "Is score direction valid and where does candidate recall leak?",
        "research_only": True,
        "inputs": {
            "direction_rows": {
                "kind": "dataset",
                "dataset_id": CONTRACT["dataset_id"],
                "contract_hash": CONTRACT["contract_hash"],
                "catalog_hash": CATALOG["catalog_hash"],
                "coverage_ratio": 1.0,
            },
            "recall_snapshot": {
                "kind": "inline",
                "schema": "discovery_recall_input_v1",
            },
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
            {
                "id": "recall",
                "operator": "discovery_recall_v1",
                "inputs": ["recall_snapshot"],
                "params": {},
            },
        ],
        "outputs": ["direction", "recall"],
    }


def _direction_rows():
    rows = []
    start = date(2026, 6, 1)
    for cohort_index in range(5):
        src = start + timedelta(days=cohort_index * 8)
        dst = src + timedelta(days=7)
        for index in range(100):
            rows.append({
                "entity_id": f"{index + 1:06d}",
                "src": src.isoformat(),
                "dst": dst.isoformat(),
                "score": float(100 - index),
                "forward_return": float(index) / 1000,
                "score_available_at": f"{src.isoformat()}T15:00:00+08:00",
                "outcome_available_at": f"{dst.isoformat()}T15:01:00+08:00",
                "snapshot_ref": "sha256:" + str(cohort_index) * 64,
            })
    return rows


def _recall_snapshot():
    return {
        "schema": "discovery_recall_input_v1",
        "quotes": [
            {"code": "600001", "name": "甲", "target_event": True},
            {"code": "600002", "name": "乙", "target_event": True},
        ],
        "prefilter_codes": ["600001"],
        "auction_codes": ["600001"],
        "executable_codes": ["600001"],
        "open_codes": ["600001"],
        "asof": "2026-07-09",
        "generated_at": "2026-07-09T15:10:00+08:00",
    }


def _inputs():
    return {
        "direction_rows": _direction_rows(),
        "recall_snapshot": _recall_snapshot(),
    }


def test_execute_plan_runs_only_whitelisted_direction_and_recall_operators(tmp_path):
    result = analysis_plan.execute_plan(
        _plan(), _inputs(), catalog=CATALOG, cache_dir=tmp_path
    )

    assert result["schema"] == "analysis_run_v1"
    assert result["research_only"] is True
    assert result["trading_action"] == "none"
    assert result["outputs"]["direction"]["verdict"] == "direction_inverted"
    assert result["outputs"]["recall"]["discovery_recall"] == 0.5
    assert result["outputs"]["recall"]["execution_gate_unchanged"] is True
    assert result["lineage"]["direction"]["operator"] == "cross_sectional_direction_v1"
    assert result["lineage"]["recall"]["output_hash"].startswith("sha256:")


def test_replay_uses_content_addressed_cache_and_preserves_outputs(tmp_path):
    first = analysis_plan.execute_plan(
        _plan(), _inputs(), catalog=CATALOG, cache_dir=tmp_path
    )
    second = analysis_plan.execute_plan(
        _plan(), _inputs(), catalog=CATALOG, cache_dir=tmp_path
    )

    assert first["cached"] is False
    assert second["cached"] is True
    assert first["cache_key"] == second["cache_key"]
    assert first["outputs"] == second["outputs"]


def test_tampered_cache_is_recomputed_instead_of_trusted(tmp_path):
    first = analysis_plan.execute_plan(
        _plan(), _inputs(), catalog=CATALOG, cache_dir=tmp_path
    )
    path = Path(first["artifact_path"])
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["outputs"]["direction"]["verdict"] = "direction_confirmed"
    path.write_text(json.dumps(tampered), encoding="utf-8")

    replay = analysis_plan.execute_plan(
        _plan(), _inputs(), catalog=CATALOG, cache_dir=tmp_path
    )

    assert replay["cached"] is False
    assert replay["outputs"]["direction"]["verdict"] == "direction_inverted"


def test_unknown_operator_is_rejected():
    plan = _plan()
    plan["nodes"][0]["operator"] = "python_eval"

    with pytest.raises(analysis_plan.AnalysisPlanError, match="operator_not_allowed:python_eval"):
        analysis_plan.validate_plan(plan, catalog=CATALOG)


def test_arbitrary_code_or_module_fields_are_rejected():
    plan = _plan()
    plan["nodes"][0]["module"] = "os"
    plan["nodes"][0]["code"] = "__import__('os').system('echo no')"

    with pytest.raises(analysis_plan.AnalysisPlanError, match="node_field_not_allowed"):
        analysis_plan.validate_plan(plan, catalog=CATALOG)


def test_cycle_is_rejected_before_execution():
    plan = _plan()
    plan["nodes"] = [
        {"id": "a", "operator": "cross_sectional_direction_v1", "inputs": ["b"], "params": {}},
        {"id": "b", "operator": "cross_sectional_direction_v1", "inputs": ["a"], "params": {}},
    ]
    plan["outputs"] = ["a"]

    with pytest.raises(analysis_plan.AnalysisPlanError, match="dependency_cycle"):
        analysis_plan.validate_plan(plan, catalog=CATALOG)


@pytest.mark.parametrize("binding,error", [
    ({"contract_hash": "sha256:" + "0" * 64}, "contract_hash_mismatch"),
    ({"catalog_hash": "sha256:" + "0" * 64}, "catalog_hash_mismatch"),
])
def test_dataset_hash_mismatch_is_rejected(binding, error):
    plan = _plan()
    plan["inputs"]["direction_rows"].update(binding)

    with pytest.raises(analysis_plan.AnalysisPlanError, match=error):
        analysis_plan.validate_plan(plan, catalog=CATALOG)


def test_dataset_record_schema_mismatch_blocks_execution(tmp_path):
    inputs = _inputs()
    inputs["direction_rows"][0]["future_feature"] = 1

    with pytest.raises(analysis_plan.AnalysisPlanError, match="unknown_field:future_feature"):
        analysis_plan.execute_plan(
            _plan(), inputs, catalog=CATALOG, cache_dir=tmp_path
        )


def test_inline_recall_schema_is_strict_and_requires_deterministic_time(tmp_path):
    inputs = _inputs()
    del inputs["recall_snapshot"]["generated_at"]

    with pytest.raises(analysis_plan.AnalysisPlanError, match="recall_input_missing:generated_at"):
        analysis_plan.execute_plan(
            _plan(), inputs, catalog=CATALOG, cache_dir=tmp_path
        )


def test_optional_recall_stage_must_be_a_list_or_null(tmp_path):
    inputs = _inputs()
    inputs["recall_snapshot"]["open_codes"] = "600001"

    with pytest.raises(analysis_plan.AnalysisPlanError, match="recall_input_type:open_codes"):
        analysis_plan.execute_plan(
            _plan(), inputs, catalog=CATALOG, cache_dir=tmp_path
        )


def test_plan_hash_changes_when_question_or_binding_changes():
    first = analysis_plan.seal_plan(_plan(), catalog=CATALOG)
    changed = copy.deepcopy(_plan())
    changed["question"] = "A different falsifiable question"
    second = analysis_plan.seal_plan(changed, catalog=CATALOG)

    assert first["plan_hash"] != second["plan_hash"]


def test_cli_executes_json_plan_and_reports_blocked_plan(tmp_path, capsys):
    plan_path = tmp_path / "plan.json"
    inputs_path = tmp_path / "inputs.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    inputs_path.write_text(json.dumps(_inputs()), encoding="utf-8")

    code = run_analysis_plan.main([
        "--plan", str(plan_path),
        "--inputs", str(inputs_path),
        "--catalog", str(ROOT / "config" / "dataset_catalog.json"),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["outputs"]["direction"]["verdict"] == "direction_inverted"

    invalid = _plan()
    invalid["nodes"][0]["operator"] = "python_eval"
    plan_path.write_text(json.dumps(invalid), encoding="utf-8")
    code = run_analysis_plan.main([
        "--plan", str(plan_path),
        "--inputs", str(inputs_path),
        "--catalog", str(ROOT / "config" / "dataset_catalog.json"),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    output = json.loads(capsys.readouterr().out)
    assert code == 2
    assert output["status"] == "blocked"
    assert "operator_not_allowed:python_eval" in output["errors"]
