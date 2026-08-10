"""Content-addressed write-back for validated, research-only derived datasets."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from paths import data_file
from state_store import atomic_write_json


SCHEMA = "derived_dataset_v1"
LINEAGE_FIELDS = {"plan_hash", "catalog_hash", "input_refs", "operator_versions"}
INPUT_REF_FIELDS = {"dataset_id", "contract_hash", "snapshot_ref"}
VALIDATION_FIELDS = {
    "schema",
    "status",
    "artifact_ref",
    "artifact_sha256",
    "records_hash",
}


class DerivedDatasetError(ValueError):
    """A derived dataset cannot satisfy the immutable write-back contract."""

    def __init__(self, *errors: str) -> None:
        self.errors = tuple(dict.fromkeys(str(error) for error in errors if error))
        super().__init__("; ".join(self.errors) or "derived_dataset_invalid")


def default_store_dir() -> str:
    return data_file("research-committee", "derived_datasets")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DerivedDatasetError("payload_not_canonical_json") from exc


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _aware(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise DerivedDatasetError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DerivedDatasetError(f"{field}_timezone_required")
    return parsed


def _sha(value: Any, field: str, *, prefixed: bool) -> str:
    text = str(value or "")
    raw = text.removeprefix("sha256:") if prefixed else text
    if (prefixed and not text.startswith("sha256:")) or len(raw) != 64 or any(
        char not in "0123456789abcdef" for char in raw.lower()
    ):
        raise DerivedDatasetError(f"{field}_invalid")
    return ("sha256:" if prefixed else "") + raw.lower()


def _strict(value: Any, allowed: set[str], prefix: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DerivedDatasetError(f"{prefix}_invalid")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise DerivedDatasetError(*(f"{prefix}_field_not_allowed:{item}" for item in unknown))
    return dict(value)


def _lineage(value: Any) -> dict[str, Any]:
    lineage = _strict(value, LINEAGE_FIELDS, "lineage")
    lineage["plan_hash"] = _sha(lineage.get("plan_hash"), "plan_hash", prefixed=True)
    lineage["catalog_hash"] = _sha(
        lineage.get("catalog_hash"), "catalog_hash", prefixed=True
    )
    refs = lineage.get("input_refs")
    if not isinstance(refs, list) or not refs:
        raise DerivedDatasetError("input_refs_missing")
    normalized_refs = []
    for ref in refs:
        item = _strict(ref, INPUT_REF_FIELDS, "input_ref")
        for field in ("dataset_id", "snapshot_ref"):
            if not str(item.get(field) or "").strip():
                raise DerivedDatasetError(f"input_ref_{field}_missing")
        item["contract_hash"] = _sha(
            item.get("contract_hash"), "contract_hash", prefixed=True
        )
        normalized_refs.append(item)
    versions = lineage.get("operator_versions")
    if not isinstance(versions, Mapping) or not versions or any(
        not str(key).strip() or not str(item).strip() for key, item in versions.items()
    ):
        raise DerivedDatasetError("operator_versions_invalid")
    return {
        **lineage,
        "input_refs": normalized_refs,
        "operator_versions": dict(sorted((str(key), str(item)) for key, item in versions.items())),
    }


def _validation(value: Any, records_hash: str) -> dict[str, Any]:
    validation = _strict(value, VALIDATION_FIELDS, "validation")
    if validation.get("schema") != "derived_dataset_validation_v1":
        raise DerivedDatasetError("validation_schema_invalid")
    if validation.get("status") != "passed":
        raise DerivedDatasetError("validation_not_passed")
    if not str(validation.get("artifact_ref") or "").strip():
        raise DerivedDatasetError("validation_artifact_ref_missing")
    validation["records_hash"] = _sha(
        validation.get("records_hash"), "validation_records_hash", prefixed=True
    )
    if validation["records_hash"] != records_hash:
        raise DerivedDatasetError("validation_records_hash_mismatch")
    declared_hash = _sha(
        validation.pop("artifact_sha256", None),
        "validation_artifact_sha256",
        prefixed=False,
    )
    expected_hash = _hash(validation).removeprefix("sha256:")
    if declared_hash != expected_hash:
        raise DerivedDatasetError("validation_artifact_hash_mismatch")
    validation["artifact_sha256"] = declared_hash
    return validation


def _records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise DerivedDatasetError("records_missing")
    if any(not isinstance(item, Mapping) for item in value):
        raise DerivedDatasetError("record_not_object")
    records = [dict(item) for item in value]
    _canonical(records)
    return records


def _verify_artifact(artifact: Any, expected_ref: str) -> dict[str, Any]:
    if not isinstance(artifact, dict) or artifact.get("schema") != SCHEMA:
        raise DerivedDatasetError("artifact_schema_invalid")
    body = {key: item for key, item in artifact.items() if key != "ref"}
    actual = _hash(body)
    if artifact.get("ref") != actual or actual != expected_ref:
        raise DerivedDatasetError("artifact_hash_mismatch")
    return artifact


def write_dataset(
    dataset_id: str,
    records: Sequence[Mapping[str, Any]],
    *,
    lineage: Mapping[str, Any],
    validation: Mapping[str, Any],
    point_in_time_cutoff: str,
    available_at: str,
    store_dir: str | None = None,
) -> dict[str, Any]:
    dataset = str(dataset_id or "").strip()
    if not dataset:
        raise DerivedDatasetError("dataset_id_missing")
    cutoff = _aware(point_in_time_cutoff, "point_in_time_cutoff")
    available = _aware(available_at, "available_at")
    if available < cutoff:
        raise DerivedDatasetError("available_before_cutoff")
    normalized_records = _records(records)
    records_hash = _hash(normalized_records)
    body = {
        "schema": SCHEMA,
        "dataset_id": dataset,
        "records": normalized_records,
        "records_hash": records_hash,
        "record_count": len(normalized_records),
        "point_in_time_cutoff": cutoff.isoformat(),
        "available_at": available.isoformat(),
        "lineage": _lineage(lineage),
        "validation": _validation(validation, records_hash),
        "research_only": True,
        "trading_action": "none",
        "admission_status": "pending_catalog_review",
    }
    ref = _hash(body)
    artifact = {**body, "ref": ref}
    directory = Path(store_dir or default_store_dir())
    path = directory / f"{ref.removeprefix('sha256:')}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DerivedDatasetError("artifact_unreadable") from exc
        _verify_artifact(existing, ref)
        return {**existing, "artifact_path": str(path), "created": False}
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(path), artifact)
    return {**artifact, "artifact_path": str(path), "created": True}


def load_dataset(ref: str, *, store_dir: str | None = None) -> dict[str, Any]:
    normalized = _sha(ref, "dataset_ref", prefixed=True)
    path = Path(store_dir or default_store_dir()) / f"{normalized.removeprefix('sha256:')}.json"
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DerivedDatasetError("artifact_unreadable") from exc
    return _verify_artifact(artifact, normalized)


__all__ = [
    "DerivedDatasetError",
    "SCHEMA",
    "default_store_dir",
    "load_dataset",
    "write_dataset",
]
