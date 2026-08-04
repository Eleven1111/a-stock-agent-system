"""Immutable evidence bindings for model-generated research output."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


MANIFEST_SCHEMA = "model_run_manifest_v1"
FINDING_APPROVAL_SCHEMA = "research_finding_approval_v1"


class ApprovalArtifactError(ValueError):
    """Trusted approval artifact violates its path or payload contract."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def finding_sha256(finding: Mapping[str, Any]) -> str:
    """Hash the model output without the manifest that carries this hash."""
    value = dict(finding)
    value.pop("model_run_manifest", None)
    return _hash(value)


_finding_hash = finding_sha256


def validate_finding_approval(
    approval: Any,
    *,
    task_id: str,
    role: str,
    claim_id: str,
    finding: Mapping[str, Any],
    submitter: str = "",
    now: str | None = None,
) -> list[str]:
    """Validate a separately produced approval against one claimed finding."""
    if not isinstance(approval, Mapping):
        return ["approval_artifact_missing"]
    reasons: list[str] = []
    if not str(task_id).strip():
        reasons.append("approval_task_context_missing")
    if not str(role).strip():
        reasons.append("approval_role_context_missing")
    if not str(claim_id).strip():
        reasons.append("approval_claim_context_missing")
    if approval.get("schema") != FINDING_APPROVAL_SCHEMA:
        reasons.append("approval_schema_invalid")
    if str(approval.get("task_id") or "") != str(task_id):
        reasons.append("approval_task_mismatch")
    if str(approval.get("role") or "") != str(role):
        reasons.append("approval_role_mismatch")
    if str(approval.get("claim_id") or "") != str(claim_id):
        reasons.append("approval_claim_mismatch")
    if str(approval.get("finding_sha256") or "") != finding_sha256(finding):
        reasons.append("approval_finding_mismatch")
    if approval.get("status") != "approved":
        reasons.append("approval_status_invalid")
    reviewer = str(approval.get("reviewer") or "").strip()
    if not reviewer:
        reasons.append("reviewer_missing")
    if submitter and reviewer == str(submitter).strip():
        reasons.append("reviewer_not_independent")
    approved_at: datetime | None = None
    try:
        approved_at = datetime.fromisoformat(str(approval.get("approved_at") or ""))
        if approved_at.tzinfo is None:
            reasons.append("approval_timestamp_invalid")
    except (TypeError, ValueError):
        reasons.append("approval_timestamp_invalid")
    try:
        checked_at = (
            datetime.fromisoformat(str(now))
            if now is not None else datetime.now(timezone.utc)
        )
        if checked_at.tzinfo is None:
            raise ValueError
        if approved_at is not None and approved_at.tzinfo is not None:
            if approved_at > checked_at:
                reasons.append("approval_from_future")
    except (TypeError, ValueError):
        reasons.append("approval_check_timestamp_invalid")
    finding_generated_at = finding.get("generated_at")
    if finding_generated_at is not None:
        try:
            generated = datetime.fromisoformat(str(finding_generated_at))
            if generated.tzinfo is None:
                raise ValueError
            if approved_at is not None and approved_at.tzinfo is not None:
                if approved_at < generated:
                    reasons.append("approval_predates_finding")
        except (TypeError, ValueError):
            reasons.append("finding_timestamp_invalid")
    return list(dict.fromkeys(reasons))


def load_trusted_finding_approval(path: str) -> dict[str, Any]:
    """Read an approval only from the state-root approval authority."""
    from paths import hermes_home

    root = os.path.abspath(os.path.join(
        os.path.expanduser(hermes_home()),
        "approvals",
        "research-committee",
    ))
    candidate = os.path.abspath(os.path.expanduser(path))
    real_root = os.path.realpath(root)
    real_candidate = os.path.realpath(candidate)
    try:
        lexically_scoped = os.path.commonpath([root, candidate]) == root
        resolved_scoped = (
            os.path.commonpath([real_root, real_candidate]) == real_root
        )
    except ValueError:
        lexically_scoped = False
        resolved_scoped = False
    if not lexically_scoped or not resolved_scoped:
        raise ApprovalArtifactError("approval_path_untrusted")
    cursor = root
    for part in os.path.relpath(candidate, root).split(os.sep):
        cursor = os.path.join(cursor, part)
        if os.path.islink(cursor):
            raise ApprovalArtifactError("approval_path_symlink_rejected")
    descriptor = os.open(
        candidate,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ApprovalArtifactError("approval_artifact_invalid")
    return value


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
    finding: Mapping[str, Any] | None = None,
    approval: Mapping[str, Any] | None = None,
    approval_ref: str = "",
    task_id: str = "",
    role: str = "",
    claim_id: str = "",
    submitter: str = "",
    review_status: str = "unreviewed",
    reviewed_by: str = "",
) -> dict[str, Any]:
    reasons = validate_reference_paths(evidence_pack, evidence_refs)
    if not str(model).strip():
        reasons.append("model_version_missing")
    approval_reasons = validate_finding_approval(
        approval,
        task_id=task_id,
        role=role,
        claim_id=claim_id,
        finding=finding or {},
        submitter=submitter,
        now=generated_at,
    )
    reasons.extend(approval_reasons)
    if approval is not None:
        if not approval_ref:
            reasons.append("approval_ref_missing")
        else:
            try:
                if load_trusted_finding_approval(approval_ref) != dict(approval):
                    reasons.append("approval_artifact_mismatch")
            except (OSError, ApprovalArtifactError, json.JSONDecodeError):
                reasons.append("approval_path_untrusted")
    approval_ready = not approval_reasons
    if not approval_ready:
        reasons.append("human_review_required")
    derived_review_status = "reviewed" if approval_ready else "unreviewed"
    derived_reviewer = (
        str((approval or {}).get("reviewer") or "") if approval_ready else ""
    )
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
        "review_status": derived_review_status,
        "reviewed_by": derived_reviewer,
        "approval": dict(approval) if isinstance(approval, Mapping) else None,
        "approval_ref": str(approval_ref),
        "approval_sha256": _hash(approval) if isinstance(approval, Mapping) else None,
        "reasons": list(dict.fromkeys(reasons)),
    }
    if finding is not None:
        manifest["output_sha256"] = _finding_hash(finding)
        manifest["context_sha256"] = manifest["input_sha256"]
    manifest["execution_eligible"] = not manifest["reasons"]
    manifest["manifest_sha256"] = _hash(manifest)
    return manifest


def _manifest_binding_reasons(
    manifest: Mapping[str, Any],
    evidence_pack: Mapping[str, Any],
    evidence_refs: Sequence[Any],
    tool_inputs: Mapping[str, Any] | None,
) -> list[str]:
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
    expected_input = _hash({"pack": evidence_pack, "tools": tool_inputs})
    if tool_inputs is not None and manifest.get("input_sha256") != expected_input:
        reasons.append("input_hash_mismatch")
    return reasons


def _approval_binding_reasons(
    manifest: Mapping[str, Any],
    finding: Mapping[str, Any],
    *,
    task_id: str = "",
    role: str = "",
    claim_id: str = "",
    submitter: str = "",
    now: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    if manifest.get("output_sha256") != _finding_hash(finding):
        reasons.append("output_hash_mismatch")
    if manifest.get("context_sha256") not in (None, manifest.get("input_sha256")):
        reasons.append("context_hash_mismatch")
    approval = manifest.get("approval")
    reasons.extend(validate_finding_approval(
        approval,
        task_id=task_id,
        role=role,
        claim_id=claim_id,
        finding=finding,
        submitter=submitter,
        now=now,
    ))
    if not isinstance(approval, Mapping):
        return reasons
    if manifest.get("approval_sha256") != _hash(approval):
        reasons.append("approval_hash_mismatch")
    approval_ref = str(manifest.get("approval_ref") or "")
    if not approval_ref:
        reasons.append("approval_ref_missing")
        return reasons
    try:
        if load_trusted_finding_approval(approval_ref) != dict(approval):
            reasons.append("approval_artifact_mismatch")
    except (OSError, ApprovalArtifactError, json.JSONDecodeError):
        reasons.append("approval_path_untrusted")
    return reasons


def _manifest_authenticity_reasons(
    manifest: Mapping[str, Any],
    *,
    now: str | None,
    max_age_minutes: int | None,
) -> list[str]:
    reasons: list[str] = []
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
    reasons.extend(
        str(item)
        for item in manifest.get("reasons") or []
        if str(item) != "human_review_required"
    )
    return reasons


def _manifest_eligibility_reasons(
    manifest: Mapping[str, Any],
    existing_reasons: Sequence[str],
    *,
    require_execution_eligible: bool,
) -> list[str]:
    reasons: list[str] = []
    review_status = str(manifest.get("review_status") or "")
    if review_status not in {"unreviewed", "reviewed", "rejected"}:
        reasons.append("review_status_invalid")
    review_ready = (
        review_status == "reviewed"
        and bool(str(manifest.get("reviewed_by") or "").strip())
    )
    if review_status == "reviewed" and not review_ready:
        reasons.append("reviewer_missing")
    derived_eligible = review_ready and not existing_reasons and not reasons
    if bool(manifest.get("execution_eligible")) != derived_eligible:
        reasons.append("execution_eligibility_mismatch")
        derived_eligible = False
    if require_execution_eligible and not derived_eligible:
        if not review_ready:
            reasons.append("human_review_required")
        reasons.append("model_run_review_only")
    return reasons


def validate_finding_manifest(
    manifest: Any,
    *,
    evidence_pack: Mapping[str, Any],
    evidence_refs: Sequence[Any],
    tool_inputs: Mapping[str, Any] | None = None,
    finding: Mapping[str, Any] | None = None,
    task_id: str = "",
    role: str = "",
    claim_id: str = "",
    submitter: str = "",
    require_execution_eligible: bool = True,
    now: str | None = None,
    max_age_minutes: int | None = None,
) -> list[str]:
    if not isinstance(manifest, Mapping) or manifest.get("schema") != MANIFEST_SCHEMA:
        return ["model_run_manifest_missing"]
    reasons = _manifest_binding_reasons(
        manifest, evidence_pack, evidence_refs, tool_inputs,
    )
    if finding is not None:
        reasons.extend(_approval_binding_reasons(
            manifest,
            finding,
            task_id=task_id,
            role=role,
            claim_id=claim_id,
            submitter=submitter,
            now=now,
        ))
    reasons.extend(_manifest_authenticity_reasons(
        manifest,
        now=now,
        max_age_minutes=max_age_minutes,
    ))
    reasons.extend(_manifest_eligibility_reasons(
        manifest,
        reasons,
        require_execution_eligible=require_execution_eligible,
    ))
    return list(dict.fromkeys(reasons))
