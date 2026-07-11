"""Immutable evidence bindings for model-generated research output."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping, Sequence


MANIFEST_SCHEMA = "model_run_manifest_v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def untrusted_external_text(content: str, *, source: str) -> dict[str, str]:
    """Keep provider text structurally separate from agent instructions."""
    return {
        "trust": "untrusted_external_data",
        "source": str(source),
        "content": str(content),
    }


def _artifact_index(evidence_pack: Mapping[str, Any]) -> dict[str, str]:
    payload = evidence_pack.get("payload") or {}
    index: dict[str, str] = {}
    for item in payload.get("fact_artifacts") or []:
        if not isinstance(item, Mapping):
            continue
        artifact_id = str(item.get("artifact_id") or item.get("job_id") or "")
        digest = str(item.get("sha256") or item.get("artifact_sha256") or "")
        if artifact_id and digest:
            index[artifact_id] = digest
    return index


def validate_citations(
    evidence_pack: Mapping[str, Any],
    citations: Sequence[Mapping[str, Any]],
) -> list[str]:
    index = _artifact_index(evidence_pack)
    if not citations:
        return ["citation_unbound"]
    reasons: list[str] = []
    for citation in citations:
        artifact_id = str(citation.get("artifact_id") or "")
        digest = str(citation.get("sha256") or "")
        if artifact_id not in index:
            reasons.append("citation_unbound")
        elif digest != index[artifact_id]:
            reasons.append("artifact_hash_mismatch")
    return list(dict.fromkeys(reasons))


def validate_reference_paths(
    evidence_pack: Mapping[str, Any], references: Sequence[Any]
) -> list[str]:
    """Resolve human-readable finding references inside one hashed pack."""
    payload = evidence_pack.get("payload") or {}
    reasons: list[str] = []
    if not references:
        return ["citation_unbound"]
    artifact_ids = {
        str(item.get("job_id") or item.get("artifact_id") or "")
        for item in payload.get("fact_artifacts") or []
        if isinstance(item, Mapping)
    }
    for raw in references:
        reference = str(raw or "").strip()
        if reference.startswith("fact_artifacts."):
            if reference.split(".", 1)[1] not in artifact_ids:
                reasons.append("citation_unbound")
            continue
        cursor: Any = payload
        for part in reference.split("."):
            if not isinstance(cursor, Mapping) or part not in cursor:
                reasons.append("citation_unbound")
                break
            cursor = cursor[part]
    return list(dict.fromkeys(reasons))


def build_model_run_manifest(
    *,
    model: str,
    prompt: str,
    evidence_pack: Mapping[str, Any],
    citations: Sequence[Mapping[str, Any]],
    tool_inputs: Mapping[str, Any],
    generated_at: str,
    review_status: str = "unreviewed",
) -> dict[str, Any]:
    reasons = validate_citations(evidence_pack, citations)
    if not str(model).strip():
        reasons.append("model_version_missing")
    if review_status not in {"unreviewed", "reviewed", "rejected"}:
        reasons.append("review_status_invalid")
    elif review_status != "reviewed":
        reasons.append("human_review_required")
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "model": str(model),
        "prompt_sha256": hashlib.sha256(str(prompt).encode("utf-8")).hexdigest(),
        "evidence_pack_ref": evidence_pack.get("ref"),
        "evidence_pack_sha256": _hash(evidence_pack),
        "citations": [dict(item) for item in citations],
        "tool_input_hashes": {
            str(name): _hash(value) for name, value in sorted(tool_inputs.items())
        },
        "input_sha256": _hash({"pack": evidence_pack, "tools": tool_inputs}),
        "generated_at": generated_at,
        "review_status": review_status,
        "reasons": list(dict.fromkeys(reasons)),
    }
    manifest["execution_eligible"] = not manifest["reasons"]
    manifest["manifest_sha256"] = _hash(manifest)
    return manifest


def build_finding_manifest(
    *,
    model: str,
    prompt: str,
    evidence_pack: Mapping[str, Any],
    evidence_refs: Sequence[Any],
    tool_inputs: Mapping[str, Any],
    generated_at: str,
    review_status: str = "unreviewed",
    reviewed_by: str = "",
) -> dict[str, Any]:
    reasons = validate_reference_paths(evidence_pack, evidence_refs)
    if not str(model).strip():
        reasons.append("model_version_missing")
    if review_status not in {"unreviewed", "reviewed", "rejected"}:
        reasons.append("review_status_invalid")
    elif review_status != "reviewed":
        reasons.append("human_review_required")
    elif not str(reviewed_by).strip():
        reasons.append("reviewer_missing")
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "runner_version": "expert_runner_v2",
        "model": str(model),
        "prompt_sha256": hashlib.sha256(str(prompt).encode("utf-8")).hexdigest(),
        "evidence_pack_ref": evidence_pack.get("ref"),
        "evidence_pack_sha256": _hash(evidence_pack),
        "evidence_refs_sha256": _hash(list(evidence_refs)),
        "tool_input_hashes": {
            str(name): _hash(value) for name, value in sorted(tool_inputs.items())
        },
        "input_sha256": _hash({"pack": evidence_pack, "tools": tool_inputs}),
        "generated_at": str(generated_at),
        "review_status": review_status,
        "reviewed_by": str(reviewed_by),
        "reasons": list(dict.fromkeys(reasons)),
    }
    manifest["execution_eligible"] = not manifest["reasons"]
    manifest["manifest_sha256"] = _hash(manifest)
    return manifest


def validate_finding_manifest(
    manifest: Any,
    *,
    evidence_pack: Mapping[str, Any],
    evidence_refs: Sequence[Any],
    tool_inputs: Mapping[str, Any] | None = None,
    require_execution_eligible: bool = True,
    now: str | None = None,
    max_age_minutes: int | None = None,
) -> list[str]:
    if not isinstance(manifest, Mapping) or manifest.get("schema") != MANIFEST_SCHEMA:
        return ["model_run_manifest_missing"]
    reasons: list[str] = []
    if manifest.get("evidence_pack_ref") != evidence_pack.get("ref"):
        reasons.append("artifact_hash_mismatch")
    if manifest.get("evidence_pack_sha256") != _hash(evidence_pack):
        reasons.append("artifact_hash_mismatch")
    if manifest.get("evidence_refs_sha256") != _hash(list(evidence_refs)):
        reasons.append("citation_unbound")
    expected_tools = {
        str(name): _hash(value)
        for name, value in sorted((tool_inputs or {}).items())
    }
    if tool_inputs is not None and manifest.get("tool_input_hashes") != expected_tools:
        reasons.append("tool_input_hash_mismatch")
    if tool_inputs is not None and manifest.get("input_sha256") != _hash(
        {"pack": evidence_pack, "tools": tool_inputs}
    ):
        reasons.append("input_hash_mismatch")
    if not str(manifest.get("model") or "").strip():
        reasons.append("model_version_missing")
    unsigned = dict(manifest)
    claimed_hash = unsigned.pop("manifest_sha256", None)
    if claimed_hash != _hash(unsigned):
        reasons.append("manifest_hash_mismatch")
    if now is not None and max_age_minutes is not None:
        try:
            generated = datetime.fromisoformat(str(manifest.get("generated_at")))
            checked = datetime.fromisoformat(str(now))
            age_minutes = (checked - generated).total_seconds() / 60
            if age_minutes < 0:
                reasons.append("model_run_future")
            elif age_minutes > max_age_minutes:
                reasons.append("model_run_stale")
        except (TypeError, ValueError):
            reasons.append("model_run_timestamp_invalid")
    manifest_reasons = [str(item) for item in manifest.get("reasons") or []]
    integrity_reasons = [
        item for item in manifest_reasons if item != "human_review_required"
    ]
    reasons.extend(integrity_reasons)
    review_status = str(manifest.get("review_status") or "")
    if review_status not in {"unreviewed", "reviewed", "rejected"}:
        reasons.append("review_status_invalid")
    review_ready = (
        review_status == "reviewed"
        and bool(str(manifest.get("reviewed_by") or "").strip())
    )
    if review_status == "reviewed" and not review_ready:
        reasons.append("reviewer_missing")
    derived_eligible = review_ready and not reasons
    if bool(manifest.get("execution_eligible")) != derived_eligible:
        reasons.append("execution_eligibility_mismatch")
        derived_eligible = False
    if require_execution_eligible and not derived_eligible:
        if not review_ready:
            reasons.append("human_review_required")
        reasons.append("model_run_review_only")
    return list(dict.fromkeys(reasons))
