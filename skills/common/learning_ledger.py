"""Append-only learning cases for governed agent-harness improvement.

The ledger records candidate failures and human review decisions.  It never
changes prompts, runtime configuration, strategy state, or the fact plane.
Accepted cases are eligible only for materialisation into an offline benchmark.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any, Mapping

from paths import data_file
from state_store import file_lock


EVENT_SCHEMA = "learning_event_v1"
CASE_SCHEMA = "learning_case_v1"
BENCHMARK_SCHEMA = "agent_harness_learning_case_v1"
EVENT_TYPES = {"learning.case.proposed", "learning.case.reviewed"}
DECISIONS = {"accepted", "rejected"}
SOURCE_FIELDS = {"kind", "artifact_ref", "artifact_sha256"}
OBSERVATION_FIELDS = {"classification", "status", "reason_codes"}
REPRODUCTION_FIELDS = {"runtime", "role", "evidence_pack_ref", "frozen_at"}
BENCHMARK_FIELDS = {
    "frozen_now",
    "category",
    "role",
    "evidence_pack",
    "payload",
    "expected_status",
    "expected_reason_codes",
    "note",
}
_REASON_CODE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")


class LearningCaseError(ValueError):
    """A learning event violates the bounded append-only contract."""

    def __init__(self, *errors: str) -> None:
        self.errors = tuple(dict.fromkeys(str(error) for error in errors if error))
        super().__init__("; ".join(self.errors) or "learning_case_invalid")


def default_ledger_file() -> str:
    return data_file("research-committee", os.path.join("learning", "learning_ledger.jsonl"))


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _timestamp(value: Any, field: str) -> str:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LearningCaseError(f"{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise LearningCaseError(f"{field}_timezone_required")
    return text


def _strict(mapping: Any, allowed: set[str], prefix: str) -> dict[str, Any]:
    if not isinstance(mapping, Mapping):
        raise LearningCaseError(f"{prefix}_invalid")
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise LearningCaseError(*(f"{prefix}_field_not_allowed:{key}" for key in unknown))
    return dict(mapping)


def _reason_codes(value: Any, prefix: str = "reason_code") -> list[str]:
    if not isinstance(value, list):
        raise LearningCaseError(f"{prefix}s_invalid")
    result: list[str] = []
    for item in value:
        code = str(item or "")
        if not _REASON_CODE.fullmatch(code):
            raise LearningCaseError(f"{prefix}_invalid:{code[:64]}")
        if code not in result:
            result.append(code)
    return result


def _validate_source(value: Any) -> dict[str, Any]:
    source = _strict(value, SOURCE_FIELDS, "source")
    for field in ("kind", "artifact_ref"):
        if not str(source.get(field) or "").strip():
            raise LearningCaseError(f"source_{field}_missing")
    digest = str(source.get("artifact_sha256") or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
        raise LearningCaseError("source_artifact_sha256_invalid")
    source["artifact_sha256"] = digest.lower()
    return source


def _validate_observation(value: Any) -> dict[str, Any]:
    observation = _strict(value, OBSERVATION_FIELDS, "observation")
    for field in ("classification", "status"):
        if not str(observation.get(field) or "").strip():
            raise LearningCaseError(f"observation_{field}_missing")
    observation["reason_codes"] = _reason_codes(observation.get("reason_codes"))
    return observation


def _validate_reproduction(value: Any) -> dict[str, Any]:
    reproduction = _strict(value, REPRODUCTION_FIELDS, "reproduction")
    reproduction["frozen_at"] = _timestamp(
        reproduction.get("frozen_at"), "reproduction_frozen_at"
    )
    return {
        key: item
        for key, item in reproduction.items()
        if item not in (None, "") or key == "frozen_at"
    }


def _validate_benchmark(value: Any) -> dict[str, Any]:
    benchmark = _strict(value, BENCHMARK_FIELDS, "benchmark")
    missing = sorted(BENCHMARK_FIELDS - set(benchmark))
    if missing:
        raise LearningCaseError(*(f"benchmark_field_missing:{key}" for key in missing))
    benchmark["frozen_now"] = _timestamp(benchmark["frozen_now"], "benchmark_frozen_now")
    for field in ("category", "role", "expected_status"):
        if not str(benchmark.get(field) or "").strip():
            raise LearningCaseError(f"benchmark_{field}_missing")
    if benchmark["evidence_pack"] is not None and not isinstance(
        benchmark["evidence_pack"], Mapping
    ):
        raise LearningCaseError("benchmark_evidence_pack_invalid")
    if benchmark["evidence_pack"] is not None:
        fixture = dict(benchmark["evidence_pack"])
        if fixture.get("schema") != "research_evidence_pack_v1":
            raise LearningCaseError("benchmark_evidence_pack_schema_invalid")
        benchmark["evidence_pack"] = fixture
    benchmark["expected_reason_codes"] = _reason_codes(
        benchmark["expected_reason_codes"], "expected_reason_code"
    )
    benchmark["note"] = str(benchmark.get("note") or "")[:1000]
    return benchmark


def _event(event_type: str, case_id: str, payload: Mapping[str, Any], now: str) -> dict[str, Any]:
    occurred_at = _timestamp(now, "occurred_at")
    body = {
        "schema": EVENT_SCHEMA,
        "event_type": event_type,
        "case_id": case_id,
        "occurred_at": occurred_at,
        "payload": dict(payload),
    }
    event_id = f"{case_id}:{event_type}:{_hash(body)[:16]}"
    core = {**body, "event_id": event_id}
    return {**core, "event_hash": _hash(core)}


def _read_unlocked(path: str) -> list[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise LearningCaseError("ledger_read_failed") from exc
    events: list[dict[str, Any]] = []
    for index, raw in enumerate(lines, 1):
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LearningCaseError(f"ledger_corrupt_line:{index}") from exc
        if not isinstance(event, dict) or event.get("schema") != EVENT_SCHEMA:
            raise LearningCaseError(f"ledger_schema_invalid:{index}")
        if event.get("event_type") not in EVENT_TYPES:
            raise LearningCaseError(f"ledger_event_type_invalid:{index}")
        claimed = event.get("event_hash")
        core = {key: item for key, item in event.items() if key != "event_hash"}
        if claimed != _hash(core):
            raise LearningCaseError(f"ledger_hash_mismatch:{index}")
        events.append(event)
    return events


def read_events(ledger_file: str | None = None) -> list[dict[str, Any]]:
    path = ledger_file or default_ledger_file()
    with file_lock(path):
        return _read_unlocked(path)


def _append_unlocked(path: str, event: Mapping[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(_canonical(event) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _project(events: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        case_id = str(event.get("case_id") or "")
        payload = dict(event.get("payload") or {})
        if event.get("event_type") == "learning.case.proposed":
            if case_id in cases:
                raise LearningCaseError(f"duplicate_case_proposal:{case_id}")
            cases[case_id] = dict(payload)
            order.append(case_id)
            continue
        if case_id not in cases:
            raise LearningCaseError(f"review_without_proposal:{case_id}")
        cases[case_id].update(
            {
                "status": payload["decision"],
                "reviewer": payload["reviewer"],
                "reviewed_at": event["occurred_at"],
                "benchmark": payload.get("benchmark"),
                "automatic_effect": "benchmark_only"
                if payload["decision"] == "accepted"
                else "none",
            }
        )
    return [cases[case_id] for case_id in order]


def project_cases(ledger_file: str | None = None) -> list[dict[str, Any]]:
    return _project(read_events(ledger_file))


def propose_case(
    *,
    source: Mapping[str, Any],
    observation: Mapping[str, Any],
    reproduction: Mapping[str, Any],
    now: str,
    ledger_file: str | None = None,
) -> dict[str, Any]:
    source_value = _validate_source(source)
    observation_value = _validate_observation(observation)
    reproduction_value = _validate_reproduction(reproduction)
    fingerprint_body = {
        "source_artifact_sha256": source_value["artifact_sha256"],
        "observation": observation_value,
        "reproduction": reproduction_value,
    }
    fingerprint = "sha256:" + _hash(fingerprint_body)
    case_id = "lc-" + fingerprint.removeprefix("sha256:")[:20]
    proposal = {
        "schema": CASE_SCHEMA,
        "case_id": case_id,
        "fingerprint": fingerprint,
        "status": "candidate",
        "research_only": True,
        "automatic_effect": "none",
        "source": source_value,
        "observation": observation_value,
        "reproduction": reproduction_value,
        "proposed_at": _timestamp(now, "proposed_at"),
    }
    path = ledger_file or default_ledger_file()
    with file_lock(path):
        events = _read_unlocked(path)
        existing = next(
            (
                case
                for case in _project(events)
                if case.get("fingerprint") == fingerprint
            ),
            None,
        )
        if existing is not None:
            return {"created": False, "case": existing}
        _append_unlocked(path, _event("learning.case.proposed", case_id, proposal, now))
    return {"created": True, "case": proposal}


def review_case(
    case_id: str,
    *,
    decision: str,
    reviewer: str,
    benchmark: Mapping[str, Any] | None = None,
    now: str,
    ledger_file: str | None = None,
) -> dict[str, Any]:
    if decision not in DECISIONS:
        raise LearningCaseError("review_decision_invalid")
    reviewer_value = str(reviewer or "").strip()
    if not reviewer_value:
        raise LearningCaseError("reviewer_missing")
    if decision == "accepted" and benchmark is None:
        raise LearningCaseError("benchmark_required")
    benchmark_value = _validate_benchmark(benchmark) if benchmark is not None else None
    path = ledger_file or default_ledger_file()
    with file_lock(path):
        events = _read_unlocked(path)
        cases = _project(events)
        if not any(case.get("case_id") == case_id for case in cases):
            raise LearningCaseError("case_not_found")
        payload = {
            "decision": decision,
            "reviewer": reviewer_value,
            "benchmark": benchmark_value,
            "effect": "benchmark_only" if decision == "accepted" else "none",
        }
        event = _event("learning.case.reviewed", case_id, payload, now)
        _append_unlocked(path, event)
        projected = _project([*events, event])
    return {
        "case": next(case for case in projected if case.get("case_id") == case_id),
        "production_changed": False,
    }


__all__ = [
    "BENCHMARK_SCHEMA",
    "CASE_SCHEMA",
    "EVENT_SCHEMA",
    "LearningCaseError",
    "default_ledger_file",
    "project_cases",
    "propose_case",
    "read_events",
    "review_case",
]
