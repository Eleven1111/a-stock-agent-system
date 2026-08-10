import hashlib
import json

import pytest

import derived_research_store as store


def _lineage():
    return {
        "plan_hash": "sha256:" + "a" * 64,
        "catalog_hash": "sha256:" + "b" * 64,
        "input_refs": [
            {
                "dataset_id": "cross_sectional_direction_rows_v1",
                "contract_hash": "sha256:" + "c" * 64,
                "snapshot_ref": "snapshot-20260808",
            }
        ],
        "operator_versions": {"group_direction_cohorts_v1": "v1"},
    }


def _hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validation(records):
    body = {
        "schema": "derived_dataset_validation_v1",
        "status": "passed",
        "artifact_ref": "validation/result.json",
        "records_hash": "sha256:" + _hash(records),
    }
    return {**body, "artifact_sha256": _hash(body)}


def test_write_is_content_addressed_idempotent_and_research_only(tmp_path):
    records = [{"src": "2026-07-01", "dst": "2026-07-08", "direction": "positive"}]
    first = store.write_dataset(
        "direction_summary_v1",
        records,
        lineage=_lineage(),
        validation=_validation(records),
        point_in_time_cutoff="2026-08-08T15:00:00+08:00",
        available_at="2026-08-10T09:30:00+08:00",
        store_dir=str(tmp_path),
    )
    second = store.write_dataset(
        "direction_summary_v1",
        records,
        lineage=_lineage(),
        validation=_validation(records),
        point_in_time_cutoff="2026-08-08T15:00:00+08:00",
        available_at="2026-08-10T09:30:00+08:00",
        store_dir=str(tmp_path),
    )

    assert first["ref"] == second["ref"]
    assert first["artifact_path"] == second["artifact_path"]
    assert first["created"] is True
    assert second["created"] is False
    loaded = store.load_dataset(first["ref"], store_dir=str(tmp_path))
    assert loaded["research_only"] is True
    assert loaded["trading_action"] == "none"
    assert loaded["admission_status"] == "pending_catalog_review"
    assert loaded["records"] == first["records"]


@pytest.mark.parametrize(
    ("cutoff", "available", "expected"),
    [
        ("2026-08-08T15:00:00", "2026-08-10T09:30:00+08:00", "timezone_required"),
        ("2026-08-11T15:00:00+08:00", "2026-08-10T09:30:00+08:00", "available_before_cutoff"),
    ],
)
def test_write_fails_closed_on_invalid_time_order(tmp_path, cutoff, available, expected):
    records = [{"value": 1}]
    with pytest.raises(store.DerivedDatasetError, match=expected):
        store.write_dataset(
            "direction_summary_v1",
            records,
            lineage=_lineage(),
            validation=_validation(records),
            point_in_time_cutoff=cutoff,
            available_at=available,
            store_dir=str(tmp_path),
        )


def test_write_requires_passed_validation_and_bound_lineage(tmp_path):
    records = [{"value": 1}]
    invalid_validation = {**_validation(records), "status": "failed"}
    with pytest.raises(store.DerivedDatasetError, match="validation_not_passed"):
        store.write_dataset(
            "direction_summary_v1",
            records,
            lineage=_lineage(),
            validation=invalid_validation,
            point_in_time_cutoff="2026-08-08T15:00:00+08:00",
            available_at="2026-08-10T09:30:00+08:00",
            store_dir=str(tmp_path),
        )

    invalid_lineage = {**_lineage(), "plan_hash": "not-a-hash"}
    with pytest.raises(store.DerivedDatasetError, match="plan_hash_invalid"):
        store.write_dataset(
            "direction_summary_v1",
            records,
            lineage=invalid_lineage,
            validation=_validation(records),
            point_in_time_cutoff="2026-08-08T15:00:00+08:00",
            available_at="2026-08-10T09:30:00+08:00",
            store_dir=str(tmp_path),
        )


def test_tampered_artifact_is_rejected(tmp_path):
    records = [{"value": 1}]
    written = store.write_dataset(
        "direction_summary_v1",
        records,
        lineage=_lineage(),
        validation=_validation(records),
        point_in_time_cutoff="2026-08-08T15:00:00+08:00",
        available_at="2026-08-10T09:30:00+08:00",
        store_dir=str(tmp_path),
    )
    path = tmp_path / f"{written['ref'].removeprefix('sha256:')}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["records"][0]["value"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(store.DerivedDatasetError, match="artifact_hash_mismatch"):
        store.load_dataset(written["ref"], store_dir=str(tmp_path))


def test_validation_must_be_cryptographically_bound_to_exact_records(tmp_path):
    validated_records = [{"value": 1}]

    with pytest.raises(store.DerivedDatasetError, match="validation_records_hash_mismatch"):
        store.write_dataset(
            "direction_summary_v1",
            [{"value": 2}],
            lineage=_lineage(),
            validation=_validation(validated_records),
            point_in_time_cutoff="2026-08-08T15:00:00+08:00",
            available_at="2026-08-10T09:30:00+08:00",
            store_dir=str(tmp_path),
        )
