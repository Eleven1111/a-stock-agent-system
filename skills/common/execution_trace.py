"""Append-only execution trace spanning one scheduled run.

Scheduling logs, job artifacts, the signal ledger and delivery telemetry were
all individually correlatable but never formed a single view of one run. This
module adds that view as a bounded, append-only event stream: dispatcher claim,
job start, gate decisions, bounded agent turns, job end and delivery attempts.

Three properties are deliberate and load-bearing:

- **The trace is not a fact ledger.** ``signal_ledger.jsonl`` keeps ownership of
  business events. A trace write failure degrades to an explicit
  ``trace_degraded`` warning and never changes a gate, a recommendation or a
  delivery decision.
- **Delivery has three distinct states.** "the process returned success",
  "the channel accepted the request" and "the user actually received it" are
  different facts. Without a real receipt callback there is no
  ``delivery.received`` event type at all, so no code path can fabricate one.
- **The field set is a strict allowlist.** Prompts, stdout, stderr, secrets and
  external response bodies have no representable slot, which is what makes the
  "zero sensitive fields in the trace" check cheap and permanent.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from paths import hermes_home
from state_store import file_lock


SCHEMA = "a_stock_execution_event_v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")

TRACE_ID_ENV = "A_STOCK_TRACE_ID"
SWITCH_ENV = "A_STOCK_EXECUTION_TRACE"
PATH_ENV = "A_STOCK_EXECUTION_TRACE_PATH"

EVENT_TYPES = (
    "dispatch.claimed",
    "job.started",
    "gate.passed",
    "gate.blocked",
    "agent.started",
    "agent.finished",
    "job.finished",
    "delivery.attempted",
    "delivery.provider_accepted",
    "delivery.failed",
)

#: Events that start a unit of work, paired with their terminal counterpart.
START_EVENTS = {"job.started": "job.finished", "agent.started": "agent.finished"}
TERMINAL_EVENTS = {"job.finished", "agent.finished"}

#: A delivery receipt is a fact this system cannot observe today. Naming the
#: forbidden type keeps the gap explicit instead of letting a future caller
#: quietly invent ``delivery.received`` from a provider ack.
FORBIDDEN_EVENT_TYPES = frozenset({"delivery.received", "delivery.read"})

#: Strict allowlist. Anything outside it is a programming error, not data to
#: silently truncate — ``build_event`` raises and ``emit`` degrades.
ALLOWED_FIELDS = frozenset({
    "schema",
    "event_id",
    "event_type",
    "occurred_at",
    "trace_id",
    "batch_id",
    "run_id",
    "job_id",
    "correlation_id",
    "trading_date",
    "runtime",
    "status",
    "artifact_ref",
    "source_versions",
    "reason_codes",
    "duration_seconds",
    "attempt",
    "channel",
    "gate",
    "role",
})

MAX_VALUE_CHARS = 512
MAX_REF_CHARS = 1024
MAX_REASON_CODES = 20
MAX_REASON_CODE_CHARS = 64
MAX_SOURCE_VERSIONS = 20

_REASON_CODE_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,%d}$" % MAX_REASON_CODE_CHARS)

_SEQUENCE = 0
_DEGRADATIONS: list[dict[str, Any]] = []


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Trace collection switch. Rollback is ``A_STOCK_EXECUTION_TRACE=off``."""
    values = env if env is not None else os.environ
    raw = str(values.get(SWITCH_ENV) or "").strip().lower()
    return raw not in {"0", "off", "false", "no", "disabled"}


def trace_path(env: Optional[Mapping[str, str]] = None) -> str:
    values = env if env is not None else os.environ
    configured = values.get(PATH_ENV)
    if configured:
        return str(configured)
    return os.path.join(hermes_home(), "cron", "execution_trace.jsonl")


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def new_trace_id(prefix: str = "trace") -> str:
    stamp = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}-{stamp}-{os.getpid()}-{secrets.token_hex(3)}"


def resolve_trace_id(
    env: Optional[Mapping[str, str]] = None,
    *,
    create: bool = True,
) -> Optional[str]:
    """Inherit the ambient trace id, or mint one for a new run.

    The dispatcher mints the id and exports it; the DAG and every job runner
    below it inherit the same value, which is what lets one DAG and all of its
    dependency jobs share a ``trace_id`` while keeping distinct ``run_id``s.
    """
    values = env if env is not None else os.environ
    existing = str(values.get(TRACE_ID_ENV) or "").strip()
    if existing:
        return existing[:MAX_VALUE_CHARS]
    return new_trace_id() if create else None


# --------------------------------------------------------------------------
# event construction
# --------------------------------------------------------------------------


def _short(value: Any, limit: int = MAX_VALUE_CHARS) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit]


def _clean_reason_codes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raise ValueError("reason_codes must be a string or a list of strings")
    codes: list[str] = []
    for item in raw:
        code = str(item).strip()
        if not code:
            continue
        if not _REASON_CODE_RE.match(code):
            raise ValueError(f"reason code is not a short slug: {code[:64]!r}")
        if code not in codes:
            codes.append(code)
        if len(codes) >= MAX_REASON_CODES:
            break
    return codes


def _clean_source_versions(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("source_versions must be a mapping")
    cleaned: dict[str, str] = {}
    for key, item in list(value.items())[:MAX_SOURCE_VERSIONS]:
        cleaned[_short(key, 64)] = _short(item, 128)
    return cleaned


def _next_sequence() -> int:
    global _SEQUENCE
    _SEQUENCE += 1
    return _SEQUENCE


def build_event(
    event_type: str,
    *,
    trace_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    run_id: Optional[str] = None,
    job_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    trading_date: Optional[str] = None,
    runtime: Optional[str] = None,
    status: Optional[str] = None,
    artifact_ref: Optional[str] = None,
    source_versions: Optional[Mapping[str, Any]] = None,
    reason_codes: Any = None,
    occurred_at: Optional[str] = None,
    **extra: Any,
) -> dict[str, Any]:
    """Validate and normalise one event. Raises ValueError on contract breaks."""
    if event_type in FORBIDDEN_EVENT_TYPES:
        raise ValueError(
            f"{event_type} is not observable: provider acceptance is not a user receipt"
        )
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type: {event_type!r}")
    unknown = set(extra) - ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"fields outside the trace allowlist: {sorted(unknown)}")

    occurred = occurred_at or now_iso()
    event: dict[str, Any] = {
        "schema": SCHEMA,
        "event_id": f"{run_id or job_id or 'anon'}:{event_type}:{_next_sequence()}",
        "event_type": event_type,
        "occurred_at": _short(occurred, 64),
        "trace_id": _short(trace_id) if trace_id else None,
        "batch_id": _short(batch_id) if batch_id else None,
        "run_id": _short(run_id) if run_id else None,
        "job_id": _short(job_id) if job_id else None,
        "correlation_id": _short(correlation_id) if correlation_id else None,
        "trading_date": _short(trading_date, 32) if trading_date else None,
        "runtime": _short(runtime, 64) if runtime else None,
        "status": _short(status, 64) if status else None,
        "artifact_ref": _short(artifact_ref, MAX_REF_CHARS) if artifact_ref else None,
        "source_versions": _clean_source_versions(source_versions),
        "reason_codes": _clean_reason_codes(reason_codes),
    }
    for key, value in extra.items():
        if value is None:
            continue
        if key in {"duration_seconds", "attempt"}:
            event[key] = value
        else:
            event[key] = _short(value)
    return event


def scan_sensitive_fields(event: Mapping[str, Any]) -> list[str]:
    """Return field names that must never reach the trace. Empty means clean."""
    return sorted(set(event) - ALLOWED_FIELDS)


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def _degrade(reason: str, detail: str) -> None:
    record = {
        "schema": "a_stock_trace_degraded_v1",
        "reason": reason,
        "detail": detail[:300],
        "occurred_at": now_iso(),
    }
    _DEGRADATIONS.append(record)
    print(
        f"[execution-trace] trace_degraded {reason}: {record['detail']}",
        file=sys.stderr,
        flush=True,
    )


def degradations() -> list[dict[str, Any]]:
    """In-process record of trace failures, for tests and doctor scripts."""
    return list(_DEGRADATIONS)


def reset_degradations() -> None:
    _DEGRADATIONS.clear()


def emit(event_type: str, *, path: Optional[str] = None, **fields: Any) -> Optional[dict[str, Any]]:
    """Append one event. Never raises: a broken trace must not break a job."""
    if not enabled():
        return None
    try:
        event = build_event(event_type, **fields)
    except ValueError as exc:
        _degrade("invalid_event", f"{event_type}: {exc}")
        return None
    target = path or trace_path()
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, default=str)
        with file_lock(target):
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    except (OSError, TimeoutError, ValueError) as exc:
        _degrade("write_failed", f"{event_type}: {exc}")
        return None
    return event


def delivery_attempted(*, channel: str, **ctx: Any) -> None:
    """Record that a delivery was tried, before the channel is called."""
    emit("delivery.attempted", channel=channel, **ctx)


def delivery_result(status: str, *, channel: str, **ctx: Any) -> None:
    """Map a channel result to acceptance or failure.

    ``sent`` means the provider accepted the request. It is deliberately not
    promoted to a user receipt: this system has no receipt callback, so the
    strongest observable fact stops at ``delivery.provider_accepted``.
    """
    accepted = str(status) == "sent"
    emit(
        "delivery.provider_accepted" if accepted else "delivery.failed",
        channel=channel,
        status=str(status),
        reason_codes=[] if accepted else [f"delivery_{status}"],
        **ctx,
    )


# --------------------------------------------------------------------------
# reading and reconstruction
# --------------------------------------------------------------------------


def read_events(
    path: Optional[str] = None,
    *,
    dedupe: bool = True,
    trace_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Read the trace, tolerating truncated tails, bad lines and duplicates."""
    return read_events_with_stats(path, dedupe=dedupe, trace_id=trace_id)[0]


def read_events_with_stats(
    path: Optional[str] = None,
    *,
    dedupe: bool = True,
    trace_id: Optional[str] = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    target = path or trace_path()
    stats = {"lines": 0, "events": 0, "corrupt_lines": 0, "duplicate_events": 0}
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with open(target, encoding="utf-8") as handle:
            raw_lines = handle.readlines()
    except OSError:
        return events, stats
    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue
        stats["lines"] += 1
        try:
            value = json.loads(line)
        except ValueError:
            stats["corrupt_lines"] += 1
            continue
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            stats["corrupt_lines"] += 1
            continue
        if value.get("event_type") not in EVENT_TYPES:
            stats["corrupt_lines"] += 1
            continue
        if trace_id is not None and value.get("trace_id") != trace_id:
            continue
        key = str(value.get("event_id") or "")
        if dedupe and key:
            if key in seen:
                stats["duplicate_events"] += 1
                continue
            seen.add(key)
        events.append(value)
        stats["events"] += 1
    return events, stats


def reconstruct_runs(events: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Rebuild per-``run_id`` lifecycle purely from the event stream.

    Every terminal state (ok / blocked / skipped / failed) must be derivable
    here without reading artifacts, which is the T01 acceptance criterion.
    """
    runs: dict[str, dict[str, Any]] = {}
    for event in events:
        run_id = str(event.get("run_id") or "")
        if not run_id:
            continue
        entry = runs.setdefault(run_id, {
            "run_id": run_id,
            "job_id": event.get("job_id"),
            "trace_id": event.get("trace_id"),
            "batch_id": event.get("batch_id"),
            "trading_date": event.get("trading_date"),
            "runtime": event.get("runtime"),
            "started_at": None,
            "finished_at": None,
            "status": None,
            "terminal_count": 0,
            "gate_blocked": False,
            "agent_turns": 0,
            "delivery_attempts": 0,
            "delivery_accepted": 0,
            "delivery_failed": 0,
            "reason_codes": [],
            "artifact_ref": None,
        })
        if entry.get("job_id") is None:
            entry["job_id"] = event.get("job_id")
        event_type = str(event.get("event_type"))
        if event_type == "job.started":
            entry["started_at"] = event.get("occurred_at")
        elif event_type == "job.finished":
            entry["finished_at"] = event.get("occurred_at")
            entry["status"] = event.get("status")
            entry["terminal_count"] += 1
            entry["artifact_ref"] = event.get("artifact_ref") or entry["artifact_ref"]
        elif event_type == "gate.blocked":
            entry["gate_blocked"] = True
        elif event_type == "agent.started":
            entry["agent_turns"] += 1
        elif event_type == "delivery.attempted":
            entry["delivery_attempts"] += 1
        elif event_type == "delivery.provider_accepted":
            entry["delivery_accepted"] += 1
        elif event_type == "delivery.failed":
            entry["delivery_failed"] += 1
        for code in event.get("reason_codes") or []:
            if code not in entry["reason_codes"]:
                entry["reason_codes"].append(code)
    return runs


def find_gaps(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Structural defects: missing start, missing end, duplicated terminal."""
    materialised = list(events)
    runs = reconstruct_runs(materialised)
    gaps: list[dict[str, Any]] = []
    for run_id, entry in sorted(runs.items()):
        if entry["started_at"] is None:
            gaps.append({"run_id": run_id, "job_id": entry["job_id"], "gap": "missing_start"})
        if entry["terminal_count"] == 0:
            gaps.append({"run_id": run_id, "job_id": entry["job_id"], "gap": "missing_terminal"})
        elif entry["terminal_count"] > 1:
            gaps.append({
                "run_id": run_id,
                "job_id": entry["job_id"],
                "gap": "duplicate_terminal",
                "count": entry["terminal_count"],
            })
    return gaps
