import copy
import json
from pathlib import Path

import pytest

import dataset_contract


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config" / "dataset_catalog.json"


def _contract():
    return {
        "schema": "dataset_contract_v1",
        "dataset_id": "direction_rows",
        "version": "1.0.0",
        "description": "Point-in-time score rows joined to later outcomes.",
        "provider": {"id": "internal:lifecycle", "adapter_version": "v1"},
        "source_rank": "derived_internal",
        "frequency": "per_decision_cohort",
        "timezone": "Asia/Shanghai",
        "currency": "CNY",
        "adjustment": "not_applicable",
        "point_in_time": {
            "feature_cutoff_field": "src",
            "feature_available_at_field": "score_available_at",
            "outcome_period_end_field": "dst",
            "outcome_available_at_field": "outcome_available_at",
            "snapshot_ref_field": "snapshot_ref",
        },
        "coverage": {
            "universe": "eligible_a_share_cash_equity",
            "minimum_ratio": 0.95,
            "missing_policy": "fail_closed",
        },
        "lineage": {
            "producer": "portfolio_research_history",
            "producer_version": "v1",
            "inputs": ["market_snapshot_v1", "candidate_lifecycle"],
        },
        "fields": [
            {"name": "entity_id", "type": "string", "semantic": "security_code", "unit": "text", "nullable": False},
            {"name": "src", "type": "date", "semantic": "feature_cutoff_date", "unit": "date", "nullable": False},
            {"name": "dst", "type": "date", "semantic": "outcome_period_end_date", "unit": "date", "nullable": False},
            {"name": "score", "type": "float", "semantic": "ranking_score", "unit": "unitless", "nullable": False},
            {"name": "forward_return", "type": "float", "semantic": "forward_return", "unit": "decimal_return", "nullable": False},
            {"name": "score_available_at", "type": "datetime", "semantic": "feature_available_at", "unit": "timestamp", "nullable": False},
            {"name": "outcome_available_at", "type": "datetime", "semantic": "outcome_available_at", "unit": "timestamp", "nullable": False},
            {"name": "snapshot_ref", "type": "string", "semantic": "market_snapshot_ref", "unit": "text", "nullable": False},
        ],
        "validators": ["strict_fields", "point_in_time_split", "coverage_ratio"],
        "known_limitations": ["Later outcomes are evaluation-only."],
    }


def _row():
    return {
        "entity_id": "000001",
        "src": "2026-07-02",
        "dst": "2026-07-09",
        "score": 0.8,
        "forward_return": 0.03,
        "score_available_at": "2026-07-02T15:00:00+08:00",
        "outcome_available_at": "2026-07-09T15:01:00+08:00",
        "snapshot_ref": "sha256:" + "a" * 64,
    }


def test_default_catalog_is_stable_and_resolvable():
    first = dataset_contract.load_catalog(CATALOG)
    second = dataset_contract.load_catalog(CATALOG)

    assert first["schema"] == "dataset_catalog_v1"
    assert first["catalog_hash"] == second["catalog_hash"]
    contract = dataset_contract.resolve_dataset(
        first, "cross_sectional_direction_rows_v1"
    )
    assert contract["contract_hash"].startswith("sha256:")
    assert contract["coverage"]["missing_policy"] == "fail_closed"


def test_contract_hash_is_independent_of_mapping_key_order():
    contract = _contract()
    reversed_contract = dict(reversed(list(contract.items())))

    assert dataset_contract.seal_contract(contract)["contract_hash"] == (
        dataset_contract.seal_contract(reversed_contract)["contract_hash"]
    )


def test_unknown_record_field_fails_closed():
    sealed = dataset_contract.seal_contract(_contract())

    with pytest.raises(dataset_contract.DatasetContractError, match="unknown_field:surprise"):
        dataset_contract.validate_records([{**_row(), "surprise": 1}], sealed)


def test_semantic_unit_conflict_is_rejected():
    contract = _contract()
    next(field for field in contract["fields"] if field["name"] == "forward_return")["unit"] = "CNY"

    with pytest.raises(dataset_contract.DatasetContractError, match="unit_conflict:forward_return"):
        dataset_contract.seal_contract(contract)


def test_missing_point_in_time_binding_is_rejected():
    contract = _contract()
    del contract["point_in_time"]["snapshot_ref_field"]

    with pytest.raises(dataset_contract.DatasetContractError, match="point_in_time_missing:snapshot_ref_field"):
        dataset_contract.seal_contract(contract)


def test_duplicate_dataset_ids_are_rejected():
    contract = _contract()
    catalog = {
        "schema": "dataset_catalog_v1",
        "catalog_version": "1.0.0",
        "datasets": [contract, copy.deepcopy(contract)],
    }

    with pytest.raises(dataset_contract.DatasetContractError, match="duplicate_dataset_id:direction_rows"):
        dataset_contract.seal_catalog(catalog)


def test_records_validate_types_and_point_in_time_split():
    sealed = dataset_contract.seal_contract(_contract())

    result = dataset_contract.validate_records([_row()], sealed, coverage_ratio=1.0)

    assert result == {
        "schema": "dataset_validation_v1",
        "dataset_id": "direction_rows",
        "contract_hash": sealed["contract_hash"],
        "record_count": 1,
        "coverage_ratio": 1.0,
        "status": "valid",
    }


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("score_available_at", "2026-07-03T09:00:00+08:00", "feature_available_after_cutoff"),
        ("outcome_available_at", "2026-07-08T15:00:00+08:00", "outcome_available_before_period_end"),
        ("snapshot_ref", "", "snapshot_ref_missing"),
        ("score", "high", "type_mismatch:score"),
    ],
)
def test_record_contract_violations_fail_closed(field, value, error):
    sealed = dataset_contract.seal_contract(_contract())
    row = {**_row(), field: value}

    with pytest.raises(dataset_contract.DatasetContractError, match=error):
        dataset_contract.validate_records([row], sealed, coverage_ratio=1.0)


def test_coverage_ratio_is_required_and_fails_below_contract_minimum():
    sealed = dataset_contract.seal_contract(_contract())

    with pytest.raises(dataset_contract.DatasetContractError, match="coverage_ratio_missing"):
        dataset_contract.validate_records([_row()], sealed)
    with pytest.raises(dataset_contract.DatasetContractError, match="coverage_ratio_below_minimum"):
        dataset_contract.validate_records([_row()], sealed, coverage_ratio=0.94)


def test_resolve_rejects_stale_contract_hash():
    catalog = dataset_contract.seal_catalog({
        "schema": "dataset_catalog_v1",
        "catalog_version": "1.0.0",
        "datasets": [_contract()],
    })

    with pytest.raises(dataset_contract.DatasetContractError, match="contract_hash_mismatch"):
        dataset_contract.resolve_dataset(
            catalog, "direction_rows", contract_hash="sha256:" + "0" * 64
        )


def test_catalog_file_contains_only_json_data():
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    assert raw["schema"] == "dataset_catalog_v1"
