"""Discover auditable learning candidates and materialise reviewed eval suites."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import learning_ledger
from state_store import atomic_write_json


SUITE_SCHEMA = "agent_harness_eval_cases_v1"
REPORT_SCHEMA = "learning_eval_factory_report_v1"
ELIGIBLE_STATUSES = {"blocked", "failed", "retryable_error", "abstained"}
CONFIGURATION_REASONS = {"runtime_turn_unconfigured", "model_version_unconfigured"}
EVIDENCE_REASONS = {"evidence_pack_insufficient", "evidence_insufficient"}
CONTRACT_REASONS = {"submission_rejected", "invalid_request"}


class EvalFactoryError(ValueError):
    """A learning candidate cannot be discovered or materialised safely."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _classification(status: str, reasons: list[str]) -> str:
    reason_set = set(reasons)
    if reason_set & CONFIGURATION_REASONS:
        return "configuration_gap"
    if reason_set & EVIDENCE_REASONS or status == "abstained":
        return "evidence_gap"
    if reason_set & CONTRACT_REASONS or any(
        reason.startswith(("schema_", "input_", "output_")) for reason in reasons
    ):
        return "contract_failure"
    if status in {"failed", "retryable_error"}:
        return "runtime_failure"
    return "unclassified_failure"


def discover_consumer_failures(
    run_dir: str,
    *,
    ledger_file: str | None = None,
    now: str,
) -> dict[str, int]:
    report = {"scanned": 0, "eligible": 0, "created": 0, "duplicates": 0, "invalid": 0}
    directory = Path(run_dir)
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        report["scanned"] += 1
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema") != "research_consumer_run_v1":
                raise ValueError("run_schema_invalid")
            status = str(value.get("status") or "")
            if status not in ELIGIBLE_STATUSES:
                continue
            reasons = [str(item) for item in value.get("reason_codes") or []]
            report["eligible"] += 1
            result = learning_ledger.propose_case(
                source={
                    "kind": "research_consumer_run",
                    "artifact_ref": str(path.resolve()),
                    "artifact_sha256": _file_hash(path),
                },
                observation={
                    "classification": _classification(status, reasons),
                    "status": status,
                    "reason_codes": reasons,
                },
                reproduction={
                    "runtime": value.get("runtime"),
                    "role": value.get("role"),
                    "evidence_pack_ref": value.get("evidence_pack_ref"),
                    "frozen_at": value.get("started_at"),
                },
                now=now,
                ledger_file=ledger_file,
            )
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            learning_ledger.LearningCaseError,
        ):
            report["invalid"] += 1
            continue
        report["created" if result["created"] else "duplicates"] += 1
    return report


def _materialized_case(case: Mapping[str, Any], fixture_dir: Path) -> dict[str, Any]:
    benchmark = dict(case["benchmark"])
    fixture = benchmark.pop("evidence_pack")
    if fixture is None:
        fixture_name = "__absent__"
    else:
        fixture_name = "learning-" + _hash(fixture)[:16]
        materialized_fixture = {**fixture, "ref": fixture_name}
        atomic_write_json(str(fixture_dir / f"{fixture_name}.json"), materialized_fixture)
    return {
        "id": f"learning-{case['case_id']}",
        "source_case_id": case["case_id"],
        "evidence_pack": fixture_name,
        **benchmark,
    }


def materialize_eval_suite(ledger_file: str, output_dir: str) -> dict[str, Any]:
    accepted = [
        case
        for case in learning_ledger.project_cases(ledger_file)
        if case.get("status") == "accepted" and isinstance(case.get("benchmark"), dict)
    ]
    if not accepted:
        raise EvalFactoryError("no_accepted_learning_cases")
    target = Path(output_dir)
    fixture_dir = target / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    cases = [_materialized_case(case, fixture_dir) for case in accepted]
    document = {
        "schema": SUITE_SCHEMA,
        "description": (
            "Human-reviewed learning cases materialised for deterministic offline replay; "
            "this suite has no production authority."
        ),
        "frozen_now": cases[0]["frozen_now"],
        "cases": cases,
    }
    atomic_write_json(str(target / "cases.json"), document)
    return {
        "schema": REPORT_SCHEMA,
        "cases": len(cases),
        "suite_path": str(target / "cases.json"),
        "suite_hash": "sha256:" + _hash(document),
        "production_changed": False,
    }


__all__ = [
    "EvalFactoryError",
    "discover_consumer_failures",
    "materialize_eval_suite",
]
