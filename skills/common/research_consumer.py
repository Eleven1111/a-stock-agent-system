"""One bounded, research-only consumer turn over the existing research bus.

Hermes/OpenClaw own the model session and inject a ``turn`` callable. This
module owns the deterministic plumbing around that call: claim fencing,
evidence-pack construction, runtime-contract validation, submission, optional
synthesis, and a bounded run artifact. It never loops forever and it never
writes the fact plane.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any

import agent_evidence
import agent_runtime_adapter
import evidence_pack
import research_bus
import research_synthesis
from agent_runtime_adapter import TurnCallable
from paths import data_file
from state_store import atomic_write_json


RUN_SCHEMA = "research_consumer_run_v1"
ALLOWED_TOOLS = ("read_evidence_pack",)
ALLOWED_STATE_READS = ("evidence_pack",)


def _now(value: str | None = None) -> str:
    text = value or datetime.now().astimezone().isoformat(timespec="seconds")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("now must include a timezone offset")
    return text


def _queue_metrics(*, now: str) -> dict[str, Any]:
    tasks = research_bus.load_tasks()
    summary = research_bus.queue_summary()
    current = datetime.fromisoformat(now)
    ages: list[int] = []
    for task in tasks:
        if task.get("status") not in research_bus.ACTIVE_STATUSES:
            continue
        try:
            created = datetime.fromisoformat(str(task.get("created_at") or ""))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=current.tzinfo)
        ages.append(max(0, int((current - created).total_seconds())))
    return {
        "total": summary["total"],
        "by_status": summary["by_status"],
        "active_count": len(summary["active"]),
        "oldest_active_age_seconds": max(ages) if ages else None,
    }


def _run_path(worker: str, now: str) -> str:
    stamp = now.replace(":", "").replace("-", "")
    safe_worker = "".join(
        char if char.isalnum() or char in "._-" else "_" for char in worker
    )
    name = f"{stamp}-{safe_worker}-{uuid.uuid4().hex[:12]}.json"
    return data_file(research_bus.SKILL, os.path.join("consumer_runs", name))


def _finish(
    outcome: dict[str, Any],
    *,
    runtime: str,
    worker: str,
    started_at: str,
    queue_before: dict[str, Any],
) -> dict[str, Any]:
    artifact = {
        "schema": RUN_SCHEMA,
        "research_only": True,
        "trading_action": "none",
        "runtime": runtime,
        "worker": worker,
        "started_at": started_at,
        "finished_at": started_at,
        "queue_before": queue_before,
        "queue_after": _queue_metrics(now=started_at),
        **outcome,
    }
    path = _run_path(worker, started_at)
    atomic_write_json(path, artifact)
    return {**artifact, "artifact_path": path}


def _ensure_pack(
    task: dict[str, Any],
    *,
    config: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    ref = str(task.get("evidence_pack_ref") or "")
    stored = evidence_pack.load_pack(ref) if ref else None
    if stored:
        return {
            "ref": ref,
            "payload": stored.get("payload") or {},
            "quality": (stored.get("payload") or {}).get("quality") or {},
            "stored": stored,
        }
    built = evidence_pack.build_pack(task, config=config, now=now)
    research_bus.update_task(str(task.get("id")), {"evidence_pack_ref": built["ref"]})
    task["evidence_pack_ref"] = built["ref"]
    return {**built, "stored": evidence_pack.load_pack(built["ref"]) or built}


def _abstain_for_pack(
    task: dict[str, Any],
    role: str,
    claim_id: str,
    worker: str,
    quality: dict[str, Any],
    *,
    config: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    finding = {
        "schema": research_bus.FINDING_SCHEMA,
        "task_id": task.get("id"),
        "role": role,
        "stance": "abstain",
        "confidence": 1.0,
        "summary": "证据包未达最低要求，shadow consumer 按 fail-closed 弃权。",
        "abstain_reason": f"evidence_pack_insufficient: missing={quality.get('missing')}",
    }
    submitted = research_bus.submit_finding(
        str(task.get("id")),
        role,
        finding,
        worker=worker,
        claim_id=claim_id,
        config=config,
        now=now,
    )
    synthesis = None
    if submitted.get("ok") and submitted.get("all_roles_done"):
        synthesis = research_synthesis.synthesize_task(
            str(task.get("id")), config=config, now=now
        )
    return {
        "status": "abstained" if submitted.get("ok") else "blocked",
        "task_id": task.get("id"),
        "role": role,
        "claim_id": claim_id,
        "reason_codes": ["evidence_pack_insufficient"],
        "submit": submitted,
        "synthesis": synthesis,
    }


def _attach_manifest(
    finding: dict[str, Any],
    *,
    request: Any,
    pack: dict[str, Any],
    model: str,
    worker: str,
    generated_at: str,
) -> None:
    finding["model_run_manifest"] = agent_evidence.build_finding_manifest(
        model=model,
        prompt=json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True),
        evidence_pack=pack,
        evidence_refs=finding.get("evidence_refs") or [],
        tool_inputs=finding.get("tool_inputs") or {},
        generated_at=generated_at,
        finding=finding,
        task_id=request.task_id,
        role=request.role,
        claim_id=str(request.claim_id or ""),
        submitter=worker,
    )


def _build_request(
    task: dict[str, Any],
    role: str,
    claim_id: str,
    *,
    runtime: str,
    model: str,
    config: dict[str, Any],
) -> Any:
    limits = config.get("finding") or {}
    role_config = (config.get("experts") or {}).get(role) or {}
    return agent_runtime_adapter.build_request(
        task,
        role,
        runtime=runtime,
        allowed_tools=ALLOWED_TOOLS,
        allowed_state_reads=ALLOWED_STATE_READS,
        max_output_chars=min(
            int(limits.get("max_finding_chars") or 10000),
            int(role_config.get("max_output_chars") or 10000) * 2,
        ),
        claim_id=claim_id,
        model=model,
    )


def _release_rejected_submission(
    submitted: dict[str, Any],
    *,
    task_id: str,
    role: str,
    worker: str,
    claim_id: str,
    config: dict[str, Any],
    now: str,
) -> tuple[list[str], dict[str, Any]]:
    errors = [str(item) for item in submitted.get("errors") or []]
    released = research_bus.fail_role(
        task_id,
        role,
        "submission_rejected: " + "; ".join(errors),
        retry=False,
        worker=worker,
        claim_id=claim_id,
        config=config,
        now=now,
    )
    return ["submission_rejected", *errors], released


def _consumer_status(run_status: str, submitted: dict[str, Any]) -> str:
    if run_status in {"completed", "abstained"} and not submitted.get("ok"):
        return "blocked"
    if run_status == "completed" and submitted.get("ok"):
        return "submitted"
    if run_status == "abstained" and submitted.get("ok"):
        return "abstained"
    if run_status == "blocked":
        return "blocked"
    if submitted.get("role_status") == "pending":
        return "retryable_error"
    return "failed"


def _consume_claimed(
    claimed: dict[str, Any],
    *,
    adapter: Any,
    runtime: str,
    worker: str,
    model: str,
    config: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    task = claimed["task"]
    role = str(claimed["role"])
    claim_id = str(claimed["claim_id"])
    pack = _ensure_pack(task, config=config, now=now)
    quality = pack.get("quality") or {}
    if quality.get("status") == "insufficient":
        return _abstain_for_pack(
            task, role, claim_id, worker, quality, config=config, now=now
        )

    request = _build_request(
        task, role, claim_id, runtime=runtime, model=model, config=config
    )
    stored_pack = pack.get("stored") or pack
    run_result = adapter.run(request, evidence_pack=stored_pack, now=now)
    if run_result.finding is not None and run_result.status == "completed":
        _attach_manifest(
            run_result.finding,
            request=request,
            pack=stored_pack,
            model=model,
            worker=worker,
            generated_at=run_result.finished_at or now,
        )
    submitted = agent_runtime_adapter.submit_result(
        run_result, worker=worker, config=config, now=now
    )
    reason_codes = list(run_result.reason_codes)
    claim_release = None
    if run_result.status in {"completed", "abstained"} and not submitted.get("ok"):
        reason_codes, claim_release = _release_rejected_submission(
            submitted,
            task_id=str(task.get("id")),
            role=role,
            worker=worker,
            claim_id=claim_id,
            config=config,
            now=now,
        )
    synthesis = None
    if submitted.get("ok") and submitted.get("all_roles_done"):
        synthesis = research_synthesis.synthesize_task(
            str(task.get("id")), config=config, now=now
        )
    return {
        "status": _consumer_status(run_result.status, submitted),
        "task_id": task.get("id"),
        "role": role,
        "claim_id": claim_id,
        "evidence_pack_ref": pack.get("ref"),
        "reason_codes": reason_codes,
        "agent_result": {
            "schema": run_result.to_dict()["schema"],
            "status": run_result.status,
            "runtime": run_result.runtime,
            "model_usage": dict(run_result.model_usage),
        },
        "submit": submitted,
        "claim_release": claim_release,
        "synthesis": synthesis,
    }


def consume_once(
    *,
    runtime: str,
    worker: str,
    turn: TurnCallable | None,
    model: str,
    roles: list[str] | None = None,
    config: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Consume at most one role and persist one auditable run artifact.

    A missing runtime callback is checked before claiming, so a partially
    configured host cannot strand a lease. Callers schedule repeated invocations
    externally; this function deliberately has no daemon or polling mode.
    """
    started_at = _now(now)
    config = config or research_bus.load_config()
    queue_before = _queue_metrics(now=started_at)
    if turn is None:
        return _finish(
            {"status": "blocked", "reason_codes": ["runtime_turn_unconfigured"]},
            runtime=runtime,
            worker=worker,
            started_at=started_at,
            queue_before=queue_before,
        )
    if not str(model).strip():
        return _finish(
            {"status": "blocked", "reason_codes": ["model_version_unconfigured"]},
            runtime=runtime,
            worker=worker,
            started_at=started_at,
            queue_before=queue_before,
        )

    adapter = agent_runtime_adapter.build_adapter(runtime, turn)
    claimed = research_bus.claim_next_work(
        worker, roles=roles, config=config, now=started_at
    )
    if not claimed:
        return _finish(
            {"status": "idle", "reason_codes": []},
            runtime=runtime,
            worker=worker,
            started_at=started_at,
            queue_before=queue_before,
        )

    return _finish(
        _consume_claimed(
            claimed,
            adapter=adapter,
            runtime=runtime,
            worker=worker,
            model=model,
            config=config,
            now=started_at,
        ),
        runtime=runtime,
        worker=worker,
        started_at=started_at,
        queue_before=queue_before,
    )


__all__ = ["RUN_SCHEMA", "consume_once"]
