"""策略 manifest 契约 — 校验、密封与信任档位天花板（纯函数，不触网）"""

import json

import pytest

from strategy_manifest import (
    MANIFEST_SCHEMA,
    StrategyManifestError,
    load_manifest,
    maximum_promotion_state,
    normalize_origin,
    promotion_within_origin_ceiling,
    seal_manifest,
    validate_manifest,
)


def manifest(**overrides):
    base = {
        "schema": MANIFEST_SCHEMA,
        "strategy_id": "ext:alice:momo_rebound:v1",
        "origin": "external_user",
        "strategy_kind": "cross_sectional_score",
        "display_name": "动量回撤再启动",
        "description": "高位回撤后重新放量的横截面打分假设。",
        "author": {"name": "alice"},
        "inputs": [
            {
                "dataset_id": "cross_sectional_direction_rows_v1",
                "contract_hash": "sha256:aaa",
                "catalog_hash": "sha256:bbb",
            }
        ],
        "research_only": True,
        "created_at": "2026-08-12T09:00:00+08:00",
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


def test_valid_manifest_has_no_errors():
    assert validate_manifest(manifest()) == []


def test_seal_binds_a_content_hash_that_changes_with_content():
    sealed = seal_manifest(manifest())
    other = seal_manifest(manifest(display_name="改了名字"))

    assert sealed["manifest_hash"].startswith("sha256:")
    assert sealed["manifest_hash"] != other["manifest_hash"]
    # 重新密封同一内容必须得到同一哈希（可复现）
    assert seal_manifest(manifest())["manifest_hash"] == sealed["manifest_hash"]


def test_seal_rejects_a_declared_hash_that_does_not_match():
    sealed = seal_manifest(manifest())
    tampered = {**sealed, "display_name": "偷改的名字"}

    with pytest.raises(StrategyManifestError) as excinfo:
        seal_manifest(tampered)
    assert "manifest_hash_mismatch" in excinfo.value.errors


def test_external_author_must_declare_research_only():
    errors = validate_manifest(manifest(research_only=False))
    assert "external_user_requires_research_only" in errors

    errors_missing = validate_manifest(manifest(research_only=None))
    assert "external_user_requires_research_only" in errors_missing


def test_first_party_is_not_forced_to_research_only():
    assert validate_manifest(
        manifest(origin="first_party", research_only=None)
    ) == []


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("schema", "strategy_manifest_v2", f"schema_mismatch:{MANIFEST_SCHEMA}"),
        ("origin", "somebody_else", "origin_invalid"),
        ("strategy_kind", "regression", "strategy_kind_invalid"),
        ("author", "alice", "author_invalid"),
        ("created_at", "not-a-date", "created_at_invalid"),
        ("strategy_id", None, "required:strategy_id"),
    ],
)
def test_contract_violations_are_named(field, value, expected):
    assert expected in validate_manifest(manifest(**{field: value}))


def test_inputs_must_bind_both_contract_and_catalog_hashes():
    errors = validate_manifest(
        manifest(inputs=[{"dataset_id": "cross_sectional_direction_rows_v1"}])
    )
    assert "required:contract_hash:inputs[0]" in errors
    assert "required:catalog_hash:inputs[0]" in errors

    assert "inputs_missing" in validate_manifest(manifest(inputs=[]))


def test_origin_ceilings():
    assert maximum_promotion_state("external_user") == "shadow"
    assert maximum_promotion_state("first_party") == "live"
    assert maximum_promotion_state("trusted_contributor") == "live"

    assert promotion_within_origin_ceiling("external_user", "shadow") is True
    assert promotion_within_origin_ceiling("external_user", "research_only") is True
    for state in ("eligible_for_manual_pilot", "manual_pilot", "live"):
        assert promotion_within_origin_ceiling("external_user", state) is False
    assert promotion_within_origin_ceiling("first_party", "live") is True
    assert promotion_within_origin_ceiling("first_party", "nonsense") is False


def test_unknown_origin_reads_as_first_party_for_legacy_records():
    assert normalize_origin(None) == "first_party"
    assert normalize_origin("") == "first_party"
    assert normalize_origin("made_up") == "first_party"
    assert normalize_origin("external_user") == "external_user"


def test_load_manifest_seals_from_disk_and_names_unreadable_files(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest()), encoding="utf-8")

    assert load_manifest(path)["manifest_hash"].startswith("sha256:")

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(StrategyManifestError):
        load_manifest(broken)

    with pytest.raises(StrategyManifestError):
        load_manifest(tmp_path / "absent.json")
