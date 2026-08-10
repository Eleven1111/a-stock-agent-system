"""Bounded two-Agent handoff into the deterministic analysis-plan compiler.

The first logical Agent is the existing research committee, whose persisted
``research_proposal_v1`` is bound into a compile request.  The second logical
Agent is an ``analysis_plan_author`` role running through an existing Hermes,
OpenClaw, or fake runtime adapter.  It may select only catalog inputs and
allowlisted analysis operators; it cannot execute code or write state.

The model output is always a draft.  Only ``analysis_plan.seal_plan`` can
produce the handoff consumed by the deterministic executor.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import agent_runtime_adapter
import analysis_plan
import dataset_contract
from agent_runtime_adapter import TurnCallable
from paths import data_file
from state_store import atomic_write_json


REQUEST_SCHEMA = "research_compile_request_v1"
DRAFT_SCHEMA = "analysis_plan_draft_v1"
COMPILATION_SCHEMA = "dual_agent_compilation_v1"
COMPILER_VERSION = "dual-agent-compiler-v1"
PLAN_AGENT_ROLE = "analysis_plan_author"
ALLOWED_TOOLS = ("read_evidence_pack", "read_dataset_catalog")
ALLOWED_STATE_READS = ("evidence_pack", "dataset_catalog")
REQUEST_FIELDS = {
    "schema",
    "task_id",
    "question",
    "evidence_pack_ref",
    "research_proposal_hash",
    "catalog_hash",
    "allowed_datasets",
    "allowed_operators",
    "interaction_agent",
    "plan_agent_role",
    "research_only",
    "trading_action",
    "request_hash",
}
DRAFT_FIELDS = {
    "schema",
    "task_id",
    "role",
    "request_hash",
    "plan",
    "evidence_refs",
    "confidence",
    "summary",
}


class DualAgentCompilerError(ValueError):
    """A compile request, draft, or persisted handoff violated its contract."""

    def __init__(self, *errors: str) -> None:
        self.errors = tuple(dict.fromkeys(str(error) for error in errors if error))
        super().__init__("; ".join(self.errors) or "dual_agent_compiler_invalid")


def default_store_dir() -> str:
    return data_file("research-committee", "compiler_handoffs")


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
        raise DualAgentCompilerError("payload_not_canonical_json") from exc


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _aware(value: str | None) -> str:
    text = value or datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DualAgentCompilerError("compile_time_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DualAgentCompilerError("compile_time_timezone_required")
    return parsed.isoformat()


def _strict(value: Any, allowed: set[str], prefix: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DualAgentCompilerError(f"{prefix}_invalid")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise DualAgentCompilerError(
            *(f"{prefix}_field_not_allowed:{item}" for item in unknown)
        )
    return dict(value)


def _catalog_bindings(catalog: Mapping[str, Any]) -> list[dict[str, str]]:
    sealed = dataset_contract.seal_catalog(catalog)
    return [
        {
            "dataset_id": str(contract["dataset_id"]),
            "contract_hash": str(contract["contract_hash"]),
        }
        for contract in sealed["datasets"]
    ]


def build_compile_request(
    research_proposal: Mapping[str, Any],
    *,
    question: str,
    evidence_pack_ref: str,
    catalog: Mapping[str, Any],
) -> dict[str, Any]:
    proposal = dict(research_proposal)
    if proposal.get("schema") != "research_proposal_v1":
        raise DualAgentCompilerError("research_proposal_schema_invalid")
    task_id = str(proposal.get("task_id") or "").strip()
    if not task_id:
        raise DualAgentCompilerError("research_proposal_task_id_missing")
    if proposal.get("policy_gate_required") is not True:
        raise DualAgentCompilerError("research_proposal_policy_gate_missing")
    if not str(proposal.get("synthesis_ref") or "").strip():
        raise DualAgentCompilerError("research_proposal_synthesis_ref_missing")
    synthesis_hash = str(proposal.get("synthesis_sha256") or "")
    if len(synthesis_hash) != 64 or any(
        char not in "0123456789abcdef" for char in synthesis_hash.lower()
    ):
        raise DualAgentCompilerError("research_proposal_synthesis_hash_invalid")
    if proposal.get("live_effect") != (
        "none_until_strategy_registry_and_decision_policy_pass"
    ):
        raise DualAgentCompilerError("research_proposal_live_effect_invalid")
    question_value = str(question or "").strip()
    if not question_value:
        raise DualAgentCompilerError("question_missing")
    pack_ref = str(evidence_pack_ref or "").strip()
    if not pack_ref:
        raise DualAgentCompilerError("evidence_pack_ref_missing")
    sealed_catalog = dataset_contract.seal_catalog(catalog)
    body = {
        "schema": REQUEST_SCHEMA,
        "task_id": task_id,
        "question": question_value,
        "evidence_pack_ref": pack_ref,
        "research_proposal_hash": _hash(proposal),
        "catalog_hash": sealed_catalog["catalog_hash"],
        "allowed_datasets": _catalog_bindings(sealed_catalog),
        "allowed_operators": sorted(analysis_plan.OPERATORS),
        "interaction_agent": "existing_research_committee",
        "plan_agent_role": PLAN_AGENT_ROLE,
        "research_only": True,
        "trading_action": "none",
    }
    return {**body, "request_hash": _hash(body)}


def _request_errors(
    value: Mapping[str, Any], catalog: Mapping[str, Any]
) -> list[str]:
    try:
        request = _strict(value, REQUEST_FIELDS, "compile_request")
    except DualAgentCompilerError as exc:
        return list(exc.errors)
    body = {key: item for key, item in request.items() if key != "request_hash"}
    errors = []
    if request.get("schema") != REQUEST_SCHEMA:
        errors.append("compile_request_schema_invalid")
    if request.get("request_hash") != _hash(body):
        errors.append("compile_request_hash_mismatch")
    if request.get("research_only") is not True or request.get("trading_action") != "none":
        errors.append("compile_request_boundary_invalid")
    if request.get("plan_agent_role") != PLAN_AGENT_ROLE:
        errors.append("plan_agent_role_invalid")
    if request.get("interaction_agent") != "existing_research_committee":
        errors.append("interaction_agent_invalid")
    if request.get("catalog_hash") != catalog.get("catalog_hash"):
        errors.append("catalog_hash_mismatch")
        return list(dict.fromkeys(errors))
    try:
        bindings = _catalog_bindings(catalog)
    except dataset_contract.DatasetContractError as exc:
        errors.extend(exc.errors)
        return list(dict.fromkeys(errors))
    if request.get("allowed_datasets") != bindings:
        errors.append("allowed_datasets_mismatch")
    if request.get("allowed_operators") != sorted(analysis_plan.OPERATORS):
        errors.append("allowed_operators_mismatch")
    for field in ("task_id", "question", "evidence_pack_ref", "research_proposal_hash"):
        if not str(request.get(field) or "").strip():
            errors.append(f"compile_request_{field}_missing")
    return list(dict.fromkeys(errors))


def _agent_result_identity(result: Any) -> str:
    return _hash(
        {
            "status": result.status,
            "task_id": result.task_id,
            "role": result.role,
            "runtime": result.runtime,
            "finding": result.finding,
            "reason_codes": list(result.reason_codes),
            "tool_usage_summary": dict(result.tool_usage_summary),
            "model_usage": dict(result.model_usage),
            "claim_id": result.claim_id,
        }
    )


def _artifact(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "compilation_hash": _hash(body)}


def _blocked(
    request: Mapping[str, Any],
    *,
    runtime: str,
    model: str,
    compiled_at: str,
    reasons: list[str],
    agent_result_hash: str | None = None,
) -> dict[str, Any]:
    body = {
        "schema": COMPILATION_SCHEMA,
        "compiler_version": COMPILER_VERSION,
        "status": "blocked",
        "handoff_status": "not_created",
        "task_id": request.get("task_id"),
        "request_hash": request.get("request_hash"),
        "agent_runtime": runtime,
        "agent_model": model,
        "agent_result_hash": agent_result_hash,
        "compiled_at": compiled_at,
        "reason_codes": list(dict.fromkeys(reasons or ["compile_blocked"])),
        "research_only": True,
        "trading_action": "none",
    }
    return _artifact(body)


def _compile_draft(
    request: Mapping[str, Any],
    draft_value: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        draft = _strict(draft_value, DRAFT_FIELDS, "plan_draft")
    except DualAgentCompilerError as exc:
        return None, list(exc.errors)
    reasons = []
    if draft.get("schema") != DRAFT_SCHEMA:
        reasons.append("plan_draft_schema_invalid")
    if draft.get("task_id") != request.get("task_id"):
        reasons.append("draft_task_id_mismatch")
    if draft.get("role") != PLAN_AGENT_ROLE:
        reasons.append("draft_role_invalid")
    if draft.get("request_hash") != request.get("request_hash"):
        reasons.append("draft_request_hash_mismatch")
    if reasons:
        return None, reasons
    try:
        sealed_plan = analysis_plan.seal_plan(draft.get("plan") or {}, catalog=catalog)
    except (analysis_plan.AnalysisPlanError, dataset_contract.DatasetContractError) as exc:
        return None, list(getattr(exc, "errors", (str(exc),)))
    if sealed_plan.get("question") != request.get("question"):
        return None, ["plan_question_mismatch"]
    return sealed_plan, []


def _invoke_plan_agent(
    request: Mapping[str, Any],
    *,
    runtime: str,
    model: str,
    turn: TurnCallable,
    evidence_pack: Mapping[str, Any],
    compiled_at: str,
) -> tuple[Any | None, list[str]]:
    agent_request = agent_runtime_adapter.build_request(
        {**request, "id": request["task_id"]},
        PLAN_AGENT_ROLE,
        runtime=runtime,
        output_schema=DRAFT_SCHEMA,
        allowed_tools=ALLOWED_TOOLS,
        allowed_state_reads=ALLOWED_STATE_READS,
        max_output_chars=20_000,
        model=model,
        model_metadata={
            "compiler_version": COMPILER_VERSION,
            "compile_request": request,
        },
    )
    try:
        adapter = agent_runtime_adapter.build_adapter(runtime, turn)
    except ValueError as exc:
        return None, [str(exc)]
    return (
        adapter.run(
            agent_request,
            evidence_pack=evidence_pack,
            now=compiled_at,
        ),
        [],
    )


def _compiled_artifact(
    request: Mapping[str, Any],
    sealed_plan: Mapping[str, Any],
    *,
    runtime: str,
    model: str,
    compiled_at: str,
    agent_result_hash: str,
) -> dict[str, Any]:
    body = {
        "schema": COMPILATION_SCHEMA,
        "compiler_version": COMPILER_VERSION,
        "status": "compiled",
        "handoff_status": "ready_for_deterministic_execution",
        "task_id": request["task_id"],
        "request_hash": request["request_hash"],
        "research_proposal_hash": request["research_proposal_hash"],
        "evidence_pack_ref": request["evidence_pack_ref"],
        "catalog_hash": request["catalog_hash"],
        "agent_runtime": runtime,
        "agent_model": model,
        "agent_result_hash": agent_result_hash,
        "compiled_at": compiled_at,
        "sealed_plan": dict(sealed_plan),
        "reason_codes": [],
        "research_only": True,
        "trading_action": "none",
    }
    return _artifact(body)


def _compile_agent_result(
    request: Mapping[str, Any],
    agent_result: Any,
    *,
    catalog: Mapping[str, Any],
    runtime: str,
    model: str,
    compiled_at: str,
) -> dict[str, Any]:
    try:
        agent_result_hash = _agent_result_identity(agent_result)
    except DualAgentCompilerError:
        return _blocked(
            request,
            runtime=runtime,
            model=model,
            compiled_at=compiled_at,
            reasons=["agent_result_not_canonical"],
        )
    if agent_result.status != "completed" or not isinstance(agent_result.finding, Mapping):
        return _blocked(
            request,
            runtime=runtime,
            model=model,
            compiled_at=compiled_at,
            reasons=list(agent_result.reason_codes) or [f"agent_{agent_result.status}"],
            agent_result_hash=agent_result_hash,
        )
    sealed_plan, reasons = _compile_draft(
        request,
        agent_result.finding,
        catalog=catalog,
    )
    if reasons or sealed_plan is None:
        return _blocked(
            request,
            runtime=runtime,
            model=model,
            compiled_at=compiled_at,
            reasons=reasons,
            agent_result_hash=agent_result_hash,
        )
    return _compiled_artifact(
        request,
        sealed_plan,
        runtime=runtime,
        model=model,
        compiled_at=compiled_at,
        agent_result_hash=agent_result_hash,
    )


def run_compile_chain(
    compile_request: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any],
    runtime: str,
    model: str,
    turn: TurnCallable,
    evidence_pack: Mapping[str, Any],
    now: str | None = None,
) -> dict[str, Any]:
    """Run the plan-author role once, then deterministically seal its draft."""

    request = dict(compile_request)
    compiled_at = _aware(now)
    errors = _request_errors(request, catalog)
    if not str(model or "").strip():
        errors.append("model_version_unconfigured")
    if not isinstance(evidence_pack, Mapping):
        errors.append("evidence_pack_invalid")
    elif evidence_pack.get("ref") != request.get("evidence_pack_ref"):
        errors.append("evidence_pack_ref_mismatch")
    agent_result = None
    if not errors:
        agent_result, errors = _invoke_plan_agent(
            request,
            runtime=runtime,
            model=model,
            turn=turn,
            evidence_pack=evidence_pack,
            compiled_at=compiled_at,
        )
    if errors or agent_result is None:
        return _blocked(
            request,
            runtime=runtime,
            model=model,
            compiled_at=compiled_at,
            reasons=errors,
        )
    return _compile_agent_result(
        request,
        agent_result,
        catalog=catalog,
        runtime=runtime,
        model=model,
        compiled_at=compiled_at,
    )


def _verify_compilation(
    value: Any, expected_hash: str | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != COMPILATION_SCHEMA:
        raise DualAgentCompilerError("compilation_schema_invalid")
    body = {key: item for key, item in value.items() if key != "compilation_hash"}
    actual = _hash(body)
    if value.get("compilation_hash") != actual or (
        expected_hash is not None and actual != expected_hash
    ):
        raise DualAgentCompilerError("compilation_hash_mismatch")
    if value.get("research_only") is not True or value.get("trading_action") != "none":
        raise DualAgentCompilerError("compilation_boundary_invalid")
    return value


def verify_compilation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute a handoff identity before it crosses into an executor."""

    return _verify_compilation(dict(value))


def store_compilation(
    value: Mapping[str, Any], *, store_dir: str | None = None
) -> dict[str, Any]:
    artifact = _verify_compilation(dict(value))
    digest = artifact["compilation_hash"].removeprefix("sha256:")
    path = Path(store_dir or default_store_dir()) / f"{digest}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DualAgentCompilerError("compilation_unreadable") from exc
        _verify_compilation(existing, artifact["compilation_hash"])
        return {"created": False, "compilation": existing, "artifact_path": str(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(path), artifact)
    return {"created": True, "compilation": artifact, "artifact_path": str(path)}


def load_compilation(
    compilation_hash: str, *, store_dir: str | None = None
) -> dict[str, Any]:
    normalized = str(compilation_hash or "")
    if not normalized.startswith("sha256:") or len(normalized) != 71:
        raise DualAgentCompilerError("compilation_hash_invalid")
    path = Path(store_dir or default_store_dir()) / (
        f"{normalized.removeprefix('sha256:')}.json"
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DualAgentCompilerError("compilation_unreadable") from exc
    return _verify_compilation(value, normalized)


__all__ = [
    "COMPILATION_SCHEMA",
    "DRAFT_SCHEMA",
    "DualAgentCompilerError",
    "REQUEST_SCHEMA",
    "build_compile_request",
    "default_store_dir",
    "load_compilation",
    "run_compile_chain",
    "store_compilation",
    "verify_compilation",
]
