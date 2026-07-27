"""Input and output contract for one bounded agent turn.

Research-plane agents previously varied by runtime: what they were allowed to
read, how long they had, what a refusal looked like, and what happened when the
model returned something malformed were all decided inside each call site. This
module makes that one contract, so Hermes and OpenClaw are interchangeable and
every failure mode has a named terminal state.

The rule this file exists to enforce: **an agent turn cannot become a fact.**
A failed, blocked or timed-out turn produces no finding at all, so it can never
be merged into a synthesis as neutral or supporting evidence. Only a
``completed`` turn — with evidence references that resolve inside the pack the
turn was given — yields something the deterministic reducer will look at.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence


REQUEST_SCHEMA = "agent_run_request_v1"
RESULT_SCHEMA = "agent_run_result_v1"

STATUSES = ("completed", "abstained", "blocked", "failed")
TERMINAL_FAILURE_STATUSES = ("blocked", "failed")

#: Fact-plane state an agent turn may never write. The adapter is handed no
#: writable handle to any of these; this list is the second line of defence,
#: checked against whatever the turn reports it touched.
FACT_PLANE_WRITE_MARKERS = (
    "portfolio.json",
    "signal_ledger.jsonl",
    "hermes-cron-manifest.json",
    "monitor_registry.json",
    "candidate_lifecycle",
    "strategy_registry.json",
)

#: Keys that would turn a research finding into an execution instruction.
#: The research plane proposes; the deterministic policy layer decides. A
#: finding that tries to promote a strategy, claim live ranking weight, or wave
#: through T+1 is refused rather than partially honoured.
FORBIDDEN_FINDING_DIRECTIVES = (
    "override_t1",
    "bypass_t1",
    "skip_t1",
    "promote_strategy",
    "live_weight",
    "place_order",
    "force_advance",
)

DIRECTIONAL_STANCES = ("support", "oppose")

DEFAULT_MAX_OUTPUT_CHARS = 10000


def _now(value: Optional[str] = None) -> str:
    return value or datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class AgentRunRequest:
    """Everything a bounded research turn is allowed to know and do."""

    task_id: str
    role: str
    evidence_pack_ref: str
    output_schema: str
    runtime: str
    allowed_tools: tuple[str, ...] = ()
    allowed_state_reads: tuple[str, ...] = ()
    forbidden_state_writes: tuple[str, ...] = FACT_PLANE_WRITE_MARKERS
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    deadline: Optional[str] = None
    model: Optional[str] = None
    model_metadata: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        errors: list[str] = []
        for name in ("task_id", "role", "evidence_pack_ref", "output_schema", "runtime"):
            if not str(getattr(self, name) or "").strip():
                errors.append(f"{name} is required")
        if not isinstance(self.max_output_chars, int) or self.max_output_chars <= 0:
            errors.append("max_output_chars must be a positive int")
        if self.deadline is not None and _parse_time(self.deadline) is None:
            errors.append("deadline must be an ISO timestamp")
        if not self.forbidden_state_writes:
            errors.append("forbidden_state_writes must not be empty")
        return errors

    def expired(self, now: Optional[str] = None) -> bool:
        deadline = _parse_time(self.deadline)
        if deadline is None:
            return False
        current = _parse_time(_now(now))
        if current is None:
            return False
        if deadline.tzinfo is None or current.tzinfo is None:
            deadline = deadline.replace(tzinfo=None)
            current = current.replace(tzinfo=None)
        return current > deadline

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REQUEST_SCHEMA,
            "task_id": self.task_id,
            "role": self.role,
            "evidence_pack_ref": self.evidence_pack_ref,
            "output_schema": self.output_schema,
            "runtime": self.runtime,
            "allowed_tools": list(self.allowed_tools),
            "allowed_state_reads": list(self.allowed_state_reads),
            "forbidden_state_writes": list(self.forbidden_state_writes),
            "max_output_chars": self.max_output_chars,
            "deadline": self.deadline,
            "model": self.model,
            "model_metadata": dict(self.model_metadata),
        }


@dataclass(frozen=True)
class AgentRunResult:
    """One terminal outcome of a bounded turn. Never partially successful."""

    status: str
    task_id: str
    role: str
    runtime: str
    finding: Optional[dict[str, Any]] = None
    evidence_refs: tuple[str, ...] = ()
    confidence: Optional[float] = None
    reason_codes: tuple[str, ...] = ()
    tool_usage_summary: Mapping[str, Any] = field(default_factory=dict)
    model_usage: Mapping[str, Any] = field(default_factory=dict)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    @property
    def produced_evidence(self) -> bool:
        """Only a completed turn contributes evidence to a synthesis."""
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RESULT_SCHEMA,
            "status": self.status,
            "task_id": self.task_id,
            "role": self.role,
            "runtime": self.runtime,
            "finding": self.finding,
            "evidence_refs": list(self.evidence_refs),
            "confidence": self.confidence,
            "reason_codes": list(self.reason_codes),
            "tool_usage_summary": dict(self.tool_usage_summary),
            "model_usage": dict(self.model_usage),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def failure(
    request: AgentRunRequest,
    status: str,
    *reason_codes: str,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    tool_usage_summary: Optional[Mapping[str, Any]] = None,
    model_usage: Optional[Mapping[str, Any]] = None,
) -> AgentRunResult:
    """A terminal non-success. Carries no finding, by construction."""
    if status not in TERMINAL_FAILURE_STATUSES:
        raise ValueError(f"{status!r} is not a failure status")
    return AgentRunResult(
        status=status,
        task_id=request.task_id,
        role=request.role,
        runtime=request.runtime,
        finding=None,
        reason_codes=tuple(reason_codes),
        tool_usage_summary=dict(tool_usage_summary or {}),
        model_usage=dict(model_usage or {}),
        started_at=started_at,
        finished_at=finished_at or _now(),
    )


def _declared_writes(tool_usage: Mapping[str, Any]) -> list[str]:
    writes = tool_usage.get("state_writes")
    if isinstance(writes, str):
        return [writes]
    if isinstance(writes, Sequence):
        return [str(item) for item in writes]
    return []


def forbidden_writes(
    tool_usage: Mapping[str, Any],
    forbidden: Sequence[str],
) -> list[str]:
    """Fact-plane paths the turn claims to have written."""
    hits: list[str] = []
    for path in _declared_writes(tool_usage):
        for marker in forbidden:
            if marker and marker in path and path not in hits:
                hits.append(path)
    return hits


def disallowed_tools(
    tool_usage: Mapping[str, Any],
    allowed: Sequence[str],
) -> list[str]:
    used = tool_usage.get("tools")
    if not isinstance(used, Sequence) or isinstance(used, str):
        return []
    allowed_set = set(allowed)
    return [str(tool) for tool in used if str(tool) not in allowed_set]


def research_only_breach(finding: Mapping[str, Any]) -> Optional[str]:
    """Directives a research finding is not allowed to issue."""
    if finding.get("influences_live_ranking") is True:
        return "research_only_boundary"
    for key in FORBIDDEN_FINDING_DIRECTIVES:
        if finding.get(key):
            return "fact_plane_directive"
    return None


def evidence_discipline_breach(
    evidence_pack: Mapping[str, Any],
    finding: Mapping[str, Any],
) -> Optional[str]:
    """Fail closed on missing, stale or future-dated evidence.

    Insufficient evidence blocks any conclusion. Degraded evidence still allows
    a descriptive neutral read, but never a directional one — a data outage must
    not be interpretable as support or as absence of risk.
    """
    payload = evidence_pack.get("payload")
    quality = (payload or {}).get("quality") if isinstance(payload, Mapping) else None
    if not isinstance(quality, Mapping):
        quality = evidence_pack.get("quality") if isinstance(evidence_pack.get("quality"), Mapping) else {}
    status = str(quality.get("status") or "ok")
    stance = str(finding.get("stance") or "")
    if status == "insufficient":
        return "evidence_insufficient"
    if status == "degraded" and stance in DIRECTIONAL_STANCES:
        return "evidence_degraded_directional"
    return None


def _envelope_breach(
    tool_usage: Mapping[str, Any],
    request: AgentRunRequest,
) -> Optional[tuple[str, str]]:
    """Permission breaches that are decided before the output is even read."""
    if forbidden_writes(tool_usage, request.forbidden_state_writes):
        return ("blocked", "forbidden_state_write")
    if disallowed_tools(tool_usage, request.allowed_tools):
        return ("blocked", "tool_not_allowed")
    return None


def _abstain_breach(
    finding: Mapping[str, Any],
    request: AgentRunRequest,
) -> Optional[tuple[str, str]]:
    """A refusal still has to be a well-formed, explained refusal."""
    if len(json.dumps(finding, ensure_ascii=False, default=str)) > request.max_output_chars:
        return ("failed", "output_too_long")
    if str(finding.get("schema") or "") != request.output_schema:
        return ("failed", "schema_mismatch")
    if not str(finding.get("abstain_reason") or "").strip():
        return ("failed", "missing_abstain_reason")
    return None


def _finding_breach(
    finding: Mapping[str, Any],
    *,
    request: AgentRunRequest,
    evidence_pack: Optional[Mapping[str, Any]],
) -> Optional[tuple[str, str]]:
    """Everything that disqualifies a would-be completed finding."""
    if len(json.dumps(finding, ensure_ascii=False, default=str)) > request.max_output_chars:
        return ("failed", "output_too_long")
    if str(finding.get("schema") or "") != request.output_schema:
        return ("failed", "schema_mismatch")
    refs = [str(ref) for ref in (finding.get("evidence_refs") or ())]
    if not refs:
        return ("failed", "no_evidence_refs")
    if evidence_pack is None:
        return ("blocked", "evidence_pack_missing")
    if _unresolved_refs(evidence_pack, refs):
        return ("blocked", "evidence_ref_unresolved")
    confidence = finding.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        return ("failed", "invalid_confidence")
    overreach = research_only_breach(finding)
    if overreach:
        return ("blocked", overreach)
    discipline = evidence_discipline_breach(evidence_pack, finding)
    if discipline:
        return ("blocked", discipline)
    return None


def _success(
    request: AgentRunRequest,
    status: str,
    finding: dict[str, Any],
    **fields: Any,
) -> AgentRunResult:
    return AgentRunResult(
        status=status,
        task_id=request.task_id,
        role=request.role,
        runtime=request.runtime,
        finding=finding,
        **fields,
    )


def parse_result(
    payload: Any,
    *,
    request: AgentRunRequest,
    evidence_pack: Optional[Mapping[str, Any]] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    now: Optional[str] = None,
) -> AgentRunResult:
    """Turn a raw runtime payload into exactly one terminal result.

    Never raises. Every malformed, over-long, over-reaching or unresolvable
    output maps to a named ``blocked`` or ``failed`` state rather than to a
    quietly degraded finding.
    """
    finished = finished_at or _now(now)

    def _fail(status: str, *codes: str, **kwargs: Any) -> AgentRunResult:
        return failure(
            request, status, *codes,
            started_at=started_at, finished_at=finished, **kwargs
        )

    if request.expired(now):
        return _fail("failed", "deadline_exceeded")
    if not isinstance(payload, Mapping):
        return _fail("failed", "invalid_payload")

    tool_usage = payload.get("tool_usage_summary")
    tool_usage = dict(tool_usage) if isinstance(tool_usage, Mapping) else {}
    model_usage = payload.get("model_usage")
    model_usage = dict(model_usage) if isinstance(model_usage, Mapping) else {}

    envelope = _envelope_breach(tool_usage, request)
    if envelope:
        return _fail(*envelope, tool_usage_summary=tool_usage, model_usage=model_usage)

    status = str(payload.get("status") or "")
    if status not in STATUSES:
        return _fail("failed", "invalid_status", tool_usage_summary=tool_usage,
                     model_usage=model_usage)
    if status in TERMINAL_FAILURE_STATUSES:
        codes = tuple(str(code) for code in (payload.get("reason_codes") or ())) or (
            f"runtime_{status}",
        )
        return _fail(status, *codes, tool_usage_summary=tool_usage,
                     model_usage=model_usage)

    finding = payload.get("finding")
    if not isinstance(finding, Mapping):
        return _fail("failed", "missing_finding", tool_usage_summary=tool_usage,
                     model_usage=model_usage)
    finding = dict(finding)
    refs = tuple(str(ref) for ref in (finding.get("evidence_refs") or ()))

    usage = {"tool_usage_summary": tool_usage, "model_usage": model_usage}
    if status == "abstained":
        breach = _abstain_breach(finding, request)
        if breach:
            return _fail(*breach, **usage)
        return _success(request, "abstained", finding, reason_codes=("abstained",),
                        started_at=started_at, finished_at=finished, **usage)

    breach = _finding_breach(finding, request=request, evidence_pack=evidence_pack)
    if breach:
        return _fail(*breach, **usage)

    return _success(
        request, "completed", finding,
        evidence_refs=refs,
        confidence=float(finding.get("confidence")),
        reason_codes=tuple(str(code) for code in (payload.get("reason_codes") or ())),
        started_at=started_at, finished_at=finished, **usage
    )


def _unresolved_refs(
    evidence_pack: Mapping[str, Any],
    refs: Sequence[str],
) -> list[str]:
    try:
        import agent_evidence
    except ImportError:
        return ["evidence_validator_unavailable"]
    return agent_evidence.validate_reference_paths(evidence_pack, list(refs))


def to_research_finding(result: AgentRunResult) -> Optional[dict[str, Any]]:
    """The only path from an agent turn to the research blackboard.

    Returns ``None`` for every non-success, which is what stops a failed turn
    from being merged in as neutral evidence.
    """
    if result.status in ("completed", "abstained") and isinstance(result.finding, dict):
        return dict(result.finding)
    return None
