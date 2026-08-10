"""Versioned semantic contracts for deterministic research datasets."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


CONTRACT_SCHEMA = "dataset_contract_v1"
CATALOG_SCHEMA = "dataset_catalog_v1"
VALIDATION_SCHEMA = "dataset_validation_v1"
FIELD_TYPES = {"string", "integer", "float", "boolean", "date", "datetime"}
SOURCE_RANKS = {"primary_official", "primary_market", "derived_internal", "external_reference"}
MISSING_POLICIES = {"fail_closed", "drop_with_report"}
SEMANTIC_UNITS = {
    "feature_cutoff_date": "date",
    "outcome_period_end_date": "date",
    "ranking_score": "unitless",
    "forward_return": "decimal_return",
    "feature_available_at": "timestamp",
    "outcome_available_at": "timestamp",
    "market_snapshot_ref": "text",
    "security_code": "text",
}
PIT_KEYS = (
    "feature_cutoff_field",
    "feature_available_at_field",
    "outcome_period_end_field",
    "outcome_available_at_field",
    "snapshot_ref_field",
)


class DatasetContractError(ValueError):
    """A dataset or catalog cannot satisfy its declared semantics."""

    def __init__(self, *errors: str) -> None:
        self.errors = tuple(dict.fromkeys(str(error) for error in errors if error))
        super().__init__("; ".join(self.errors) or "dataset_contract_invalid")


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _required(mapping: Mapping[str, Any], names: Sequence[str]) -> list[str]:
    return [f"required:{name}" for name in names if not mapping.get(name)]


def _field_errors(fields: Any) -> tuple[list[str], set[str]]:
    if not isinstance(fields, list) or not fields:
        return ["fields_missing"], set()
    errors: list[str] = []
    names: set[str] = set()
    for field in fields:
        if not isinstance(field, Mapping):
            errors.append("field_invalid")
            continue
        name = str(field.get("name") or "")
        if not name:
            errors.append("field_name_missing")
            continue
        if name in names:
            errors.append(f"duplicate_field:{name}")
        names.add(name)
        if field.get("type") not in FIELD_TYPES:
            errors.append(f"field_type_invalid:{name}")
        semantic = str(field.get("semantic") or "")
        if not semantic:
            errors.append(f"field_semantic_missing:{name}")
        expected_unit = SEMANTIC_UNITS.get(semantic)
        if expected_unit and field.get("unit") != expected_unit:
            errors.append(f"unit_conflict:{name}")
        if not isinstance(field.get("nullable"), bool):
            errors.append(f"nullable_invalid:{name}")
    return errors, names


def _point_in_time_errors(value: Any, field_names: set[str]) -> list[str]:
    if not isinstance(value, Mapping):
        return ["point_in_time_missing"]
    errors = [
        f"point_in_time_missing:{key}" for key in PIT_KEYS if not value.get(key)
    ]
    errors.extend(
        f"point_in_time_field_unknown:{value[key]}"
        for key in PIT_KEYS
        if value.get(key) and value[key] not in field_names
    )
    return errors


def validate_contract(contract: Mapping[str, Any]) -> list[str]:
    errors = _required(
        contract,
        (
            "dataset_id", "version", "description", "provider", "source_rank",
            "frequency", "timezone", "currency", "adjustment", "coverage",
            "lineage", "validators", "known_limitations",
        ),
    )
    if contract.get("schema") != CONTRACT_SCHEMA:
        errors.append(f"schema_mismatch:{CONTRACT_SCHEMA}")
    if contract.get("source_rank") not in SOURCE_RANKS:
        errors.append("source_rank_invalid")
    try:
        ZoneInfo(str(contract.get("timezone") or ""))
    except (KeyError, ValueError):
        errors.append("timezone_invalid")
    provider = contract.get("provider")
    if not isinstance(provider, Mapping):
        errors.append("provider_invalid")
    else:
        errors.extend(_required(provider, ("id", "adapter_version")))
    coverage = contract.get("coverage")
    if not isinstance(coverage, Mapping):
        errors.append("coverage_invalid")
    else:
        ratio = coverage.get("minimum_ratio")
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
            errors.append("coverage_ratio_invalid")
        if coverage.get("missing_policy") not in MISSING_POLICIES:
            errors.append("missing_policy_invalid")
    lineage = contract.get("lineage")
    if not isinstance(lineage, Mapping):
        errors.append("lineage_invalid")
    else:
        errors.extend(_required(lineage, ("producer", "producer_version", "inputs")))
    field_errors, names = _field_errors(contract.get("fields"))
    errors.extend(field_errors)
    errors.extend(_point_in_time_errors(contract.get("point_in_time"), names))
    return list(dict.fromkeys(errors))


def seal_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(contract)
    declared_hash = body.pop("contract_hash", None)
    errors = validate_contract(body)
    if errors:
        raise DatasetContractError(*errors)
    actual_hash = _content_hash(body)
    if declared_hash is not None and declared_hash != actual_hash:
        raise DatasetContractError("contract_hash_mismatch")
    return {**body, "contract_hash": actual_hash}


def seal_catalog(catalog: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(catalog)
    declared_hash = body.pop("catalog_hash", None)
    if body.get("schema") != CATALOG_SCHEMA:
        raise DatasetContractError(f"schema_mismatch:{CATALOG_SCHEMA}")
    if not str(body.get("catalog_version") or ""):
        raise DatasetContractError("required:catalog_version")
    datasets = body.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise DatasetContractError("datasets_missing")
    ids = [str(item.get("dataset_id") or "") for item in datasets if isinstance(item, Mapping)]
    duplicates = sorted({dataset_id for dataset_id in ids if ids.count(dataset_id) > 1})
    if duplicates:
        raise DatasetContractError(*(f"duplicate_dataset_id:{item}" for item in duplicates))
    sealed = sorted((seal_contract(item) for item in datasets), key=lambda item: item["dataset_id"])
    normalized = {**body, "datasets": sealed}
    actual_hash = _content_hash(normalized)
    if declared_hash is not None and declared_hash != actual_hash:
        raise DatasetContractError("catalog_hash_mismatch")
    return {**normalized, "catalog_hash": actual_hash}


def load_catalog(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise DatasetContractError("catalog_not_object")
    return seal_catalog(value)


def resolve_dataset(
    catalog: Mapping[str, Any],
    dataset_id: str,
    *,
    contract_hash: str | None = None,
) -> dict[str, Any]:
    sealed = seal_catalog(catalog)
    contract = next(
        (item for item in sealed["datasets"] if item["dataset_id"] == dataset_id),
        None,
    )
    if contract is None:
        raise DatasetContractError(f"dataset_not_found:{dataset_id}")
    if contract_hash is not None and contract["contract_hash"] != contract_hash:
        raise DatasetContractError("contract_hash_mismatch")
    return contract


def _value_matches(value: Any, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "float":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    if type_name == "date":
        try:
            date.fromisoformat(str(value))
            return True
        except ValueError:
            return False
    if type_name == "datetime":
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed.tzinfo is not None and parsed.utcoffset() is not None
        except ValueError:
            return False
    return False


def _record_errors(record: Mapping[str, Any], contract: Mapping[str, Any]) -> list[str]:
    fields = {str(field["name"]): field for field in contract["fields"]}
    errors = [f"unknown_field:{name}" for name in record if name not in fields]
    for name, field in fields.items():
        value = record.get(name)
        if value is None:
            if not field["nullable"]:
                errors.append(f"required_field:{name}")
            continue
        if not _value_matches(value, str(field["type"])):
            errors.append(f"type_mismatch:{name}")
    return errors


def _pit_record_errors(record: Mapping[str, Any], contract: Mapping[str, Any]) -> list[str]:
    pit = contract["point_in_time"]
    try:
        feature_cutoff = date.fromisoformat(str(record[pit["feature_cutoff_field"]]))
        feature_available = datetime.fromisoformat(str(record[pit["feature_available_at_field"]]))
        outcome_end = date.fromisoformat(str(record[pit["outcome_period_end_field"]]))
        outcome_available = datetime.fromisoformat(str(record[pit["outcome_available_at_field"]]))
    except (KeyError, ValueError, TypeError):
        return []
    errors: list[str] = []
    if feature_available.date() > feature_cutoff:
        errors.append("feature_available_after_cutoff")
    if outcome_available.date() < outcome_end:
        errors.append("outcome_available_before_period_end")
    if outcome_end < feature_cutoff:
        errors.append("outcome_period_before_feature_cutoff")
    if not str(record.get(str(pit["snapshot_ref_field"])) or "").strip():
        errors.append("snapshot_ref_missing")
    return errors


def validate_records(
    records: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
    *,
    coverage_ratio: float | None = None,
) -> dict[str, Any]:
    sealed = seal_contract(contract)
    errors: list[str] = []
    if "coverage_ratio" in sealed["validators"]:
        if coverage_ratio is None:
            errors.append("coverage_ratio_missing")
        elif (
            isinstance(coverage_ratio, bool)
            or not isinstance(coverage_ratio, (int, float))
            or not 0 <= float(coverage_ratio) <= 1
        ):
            errors.append("coverage_ratio_invalid")
        elif float(coverage_ratio) < float(sealed["coverage"]["minimum_ratio"]):
            errors.append("coverage_ratio_below_minimum")
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            errors.append(f"record_not_object:{index}")
            continue
        errors.extend(_record_errors(record, sealed))
        errors.extend(_pit_record_errors(record, sealed))
    if errors:
        raise DatasetContractError(*errors)
    return {
        "schema": VALIDATION_SCHEMA,
        "dataset_id": sealed["dataset_id"],
        "contract_hash": sealed["contract_hash"],
        "record_count": len(records),
        "coverage_ratio": float(coverage_ratio) if coverage_ratio is not None else None,
        "status": "valid",
    }


__all__ = [
    "CATALOG_SCHEMA",
    "CONTRACT_SCHEMA",
    "DatasetContractError",
    "load_catalog",
    "resolve_dataset",
    "seal_catalog",
    "seal_contract",
    "validate_contract",
    "validate_records",
]
