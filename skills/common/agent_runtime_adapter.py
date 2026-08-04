"""Runtime adapters for bounded research-plane agent turns.

"How an agent runs" and "what a research turn is allowed to conclude" used to
be entangled at each call site, so Hermes and OpenClaw could drift apart without
anything failing. This module separates them: the adapter owns invocation,
timing, tracing and error mapping; `agent_run_contract` owns what counts as a
valid result; and the deterministic research plane still owns every fact.

Both runtimes implement the same interface and are covered by one conformance
suite driven by a fake runtime, so CI never calls a real model.

Scope is deliberately narrow — research-only. An adapter is never handed a
writable portfolio, signal ledger or cron manifest, and the single exit toward
persisted state is `submit_result`, which can only reach the research bus.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

import agent_run_contract
from agent_run_contract import AgentRunRequest, AgentRunResult


TurnCallable = Callable[[AgentRunRequest, Optional[Mapping[str, Any]]], Any]


def _now(value: Optional[str] = None) -> str:
    return agent_run_contract._now(value)


def _trace(event_type: str, **fields: Any) -> None:
    """Best-effort trace emission; observability never blocks a research turn."""
    try:
        import execution_trace
    except ImportError:
        return
    execution_trace.emit(event_type, **fields)


class AgentRuntimeAdapter:
    """Base adapter. Subclasses differ only in identity, never in semantics."""

    name = "base"

    def __init__(self, turn: TurnCallable) -> None:
        self._turn = turn

    # -- invocation ------------------------------------------------------

    def _invoke(
        self,
        request: AgentRunRequest,
        evidence_pack: Optional[Mapping[str, Any]],
    ) -> Any:
        return self._turn(request, evidence_pack)

    def run(
        self,
        request: AgentRunRequest,
        *,
        evidence_pack: Optional[Mapping[str, Any]] = None,
        now: Optional[str] = None,
    ) -> AgentRunResult:
        started_at = _now(now)
        errors = request.validate()
        if errors:
            return agent_run_contract.failure(
                request, "failed", "invalid_request", started_at=started_at
            )

        pack = evidence_pack
        if pack is None:
            pack = self._load_pack(request.evidence_pack_ref)
        if pack is None:
            result = agent_run_contract.failure(
                request, "blocked", "evidence_pack_missing", started_at=started_at
            )
            self._trace_pair(request, result, started_at)
            return result

        _trace(
            "agent.started",
            job_id=request.task_id,
            run_id=f"{request.task_id}:{request.role}",
            role=request.role,
            runtime=request.runtime,
            status="started",
        )
        try:
            payload = self._invoke(request, pack)
        except TimeoutError:
            result = agent_run_contract.failure(
                request, "failed", "deadline_exceeded", started_at=started_at
            )
        except Exception:  # noqa: BLE001 — a runtime crash is a terminal state
            result = agent_run_contract.failure(
                request, "failed", "runtime_exception", started_at=started_at
            )
        else:
            result = agent_run_contract.parse_result(
                payload,
                request=request,
                evidence_pack=pack,
                started_at=started_at,
                now=now,
            )
        _trace(
            "agent.finished",
            job_id=request.task_id,
            run_id=f"{request.task_id}:{request.role}",
            role=request.role,
            runtime=request.runtime,
            status=result.status,
            reason_codes=list(result.reason_codes),
        )
        return result

    def _trace_pair(
        self,
        request: AgentRunRequest,
        result: AgentRunResult,
        started_at: str,
    ) -> None:
        _trace(
            "agent.started",
            job_id=request.task_id,
            run_id=f"{request.task_id}:{request.role}",
            role=request.role,
            runtime=request.runtime,
            status="started",
            occurred_at=started_at,
        )
        _trace(
            "agent.finished",
            job_id=request.task_id,
            run_id=f"{request.task_id}:{request.role}",
            role=request.role,
            runtime=request.runtime,
            status=result.status,
            reason_codes=list(result.reason_codes),
        )

    @staticmethod
    def _load_pack(ref: str) -> Optional[Mapping[str, Any]]:
        try:
            import evidence_pack
        except ImportError:
            return None
        return evidence_pack.load_pack(ref)


class HermesRuntimeAdapter(AgentRuntimeAdapter):
    name = "hermes"


class OpenClawRuntimeAdapter(AgentRuntimeAdapter):
    name = "openclaw"


class FakeRuntimeAdapter(AgentRuntimeAdapter):
    """Deterministic stand-in used by conformance tests and offline evals."""

    name = "fake"


RUNTIME_ADAPTERS = {
    "hermes": HermesRuntimeAdapter,
    "openclaw": OpenClawRuntimeAdapter,
    "fake": FakeRuntimeAdapter,
}


def build_adapter(runtime: str, turn: TurnCallable) -> AgentRuntimeAdapter:
    try:
        return RUNTIME_ADAPTERS[runtime](turn)
    except KeyError as exc:
        raise ValueError(f"unknown agent runtime: {runtime!r}") from exc


def build_request(
    task: Mapping[str, Any],
    role: str,
    *,
    runtime: str,
    output_schema: str = "research_finding_v1",
    allowed_tools: tuple[str, ...] = (),
    allowed_state_reads: tuple[str, ...] = (),
    max_output_chars: int = agent_run_contract.DEFAULT_MAX_OUTPUT_CHARS,
    deadline: Optional[str] = None,
    claim_id: Optional[str] = None,
    model: Optional[str] = None,
    model_metadata: Optional[Mapping[str, Any]] = None,
) -> AgentRunRequest:
    """Build a request from a claimed research task, never from chat context."""
    return AgentRunRequest(
        task_id=str(task.get("id") or ""),
        role=role,
        evidence_pack_ref=str(task.get("evidence_pack_ref") or ""),
        output_schema=output_schema,
        runtime=runtime,
        allowed_tools=tuple(allowed_tools),
        allowed_state_reads=tuple(allowed_state_reads),
        max_output_chars=max_output_chars,
        deadline=deadline,
        claim_id=claim_id,
        model=model,
        model_metadata=dict(model_metadata or {}),
    )


def submit_result(
    result: AgentRunResult,
    *,
    worker: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    path: Optional[str] = None,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Route a terminal result to the research bus — and nowhere else.

    A successful or abstaining turn submits a finding for deterministic
    synthesis. Every failure marks the role failed. Neither path can reach the
    portfolio, the signal ledger, the candidate FSM or the cron manifest.
    """
    import research_bus

    finding = agent_run_contract.to_research_finding(result)
    if finding is None:
        return research_bus.fail_role(
            result.task_id,
            result.role,
            ",".join(result.reason_codes) or result.status,
            retry=result.status != "blocked",
            claim_id=result.claim_id,
            config=dict(config) if config else None,
            path=path,
            now=now,
        )
    return research_bus.submit_finding(
        result.task_id,
        result.role,
        finding,
        worker=worker,
        claim_id=result.claim_id,
        config=dict(config) if config else None,
        path=path,
        now=now,
    )
