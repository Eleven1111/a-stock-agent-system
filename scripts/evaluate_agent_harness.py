#!/usr/bin/env python3
"""Replay the fixed agent-harness evaluation set.

"The model seemed to answer well" is not a result. This runner replays a frozen
case set through the real `agent_runtime_adapter` contract and reports hard
pass/fail metrics.

Scope, stated plainly: the dataset checks **evidence discipline and authority
boundaries** — schema validity, evidence resolvability, fail-closed behaviour,
abstention, and the research-only boundary. It says nothing about investment
returns or open-world prediction accuracy, and it must not be cited as if it did.

Replay is deterministic: a frozen clock, fixture evidence packs, no production
state, no network, and no model call.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Mapping

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import agent_run_contract  # noqa: E402
import agent_runtime_adapter  # noqa: E402

EVAL_DIR = os.path.join(ROOT, "evals", "agent_harness")
CASES_PATH = os.path.join(EVAL_DIR, "cases.json")
FIXTURES_DIR = os.path.join(EVAL_DIR, "fixtures")
REPORT_SCHEMA = "agent_harness_eval_report_v1"

#: Case categories whose expected outcome is a refusal. Every one of them must
#: block or fail — a single leak here is a contract regression, not a metric dip.
FAIL_CLOSED_CATEGORIES = {
    "evidence_discipline",
    "evidence_refs",
    "research_boundary",
    "provider_degradation",
    "source_grading",
}


def load_cases(path: str = CASES_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_pack(name: str, *, fixtures_dir: str = FIXTURES_DIR) -> Mapping[str, Any] | None:
    if name == "__absent__":
        return None
    with open(os.path.join(fixtures_dir, f"{name}.json"), encoding="utf-8") as handle:
        return json.load(handle)


def _turn_for(payload: Any):
    def turn(request, evidence_pack):
        if isinstance(payload, Mapping) and payload.get("__raise__") == "timeout":
            raise TimeoutError("frozen timeout case")
        if isinstance(payload, Mapping) and payload.get("__raise__") == "exception":
            raise RuntimeError("frozen runtime failure case")
        return payload
    return turn


def run_case(
    case: Mapping[str, Any],
    *,
    runtime: str,
    frozen_now: str,
    fixtures_dir: str = FIXTURES_DIR,
) -> dict[str, Any]:
    pack = load_pack(str(case.get("evidence_pack")), fixtures_dir=fixtures_dir)
    overrides = dict(case.get("request") or {})
    request = agent_run_contract.AgentRunRequest(
        task_id=str(overrides.get("task_id") or "eval-task"),
        role=str(case.get("role") or "fundamental"),
        evidence_pack_ref=str(case.get("evidence_pack")),
        output_schema=str(overrides.get("output_schema") or "research_finding_v1"),
        runtime=runtime,
        allowed_tools=tuple(overrides.get("allowed_tools") or ("read_evidence_pack",)),
        allowed_state_reads=tuple(overrides.get("allowed_state_reads") or ("evidence_pack",)),
        max_output_chars=int(overrides.get("max_output_chars") or 4000),
        deadline=overrides.get("deadline"),
        model=overrides.get("model"),
    )
    adapter = agent_runtime_adapter.build_adapter(runtime, _turn_for(case.get("payload")))
    result = adapter.run(request, evidence_pack=pack, now=frozen_now)

    expected_status = str(case.get("expected_status"))
    expected_codes = list(case.get("expected_reason_codes") or [])
    status_ok = result.status == expected_status
    codes_ok = all(code in result.reason_codes for code in expected_codes)

    return {
        "id": case.get("id"),
        "category": case.get("category"),
        "runtime": runtime,
        "expected_status": expected_status,
        "actual_status": result.status,
        "expected_reason_codes": expected_codes,
        "actual_reason_codes": list(result.reason_codes),
        "passed": status_ok and codes_ok,
        "produced_finding": agent_run_contract.to_research_finding(result) is not None,
        "note": case.get("note") or "",
    }


def evaluate(
    *,
    cases_path: str = CASES_PATH,
    runtimes: tuple[str, ...] = ("hermes", "openclaw"),
) -> dict[str, Any]:
    document = load_cases(cases_path)
    frozen_now = str(document.get("frozen_now"))
    cases = document.get("cases") or []
    fixtures_dir = os.path.join(os.path.dirname(os.path.abspath(cases_path)), "fixtures")
    results = [
        run_case(
            case,
            runtime=runtime,
            frozen_now=str(case.get("frozen_now") or frozen_now),
            fixtures_dir=fixtures_dir,
        )
        for case in cases
        for runtime in runtimes
    ]

    fail_closed = [
        row for row in results
        if row["category"] in FAIL_CLOSED_CATEGORIES
        and row["expected_status"] in ("blocked", "failed")
    ]
    abstain_cases = [row for row in results if row["category"] == "abstain"]
    boundary_cases = [row for row in results if row["category"] == "research_boundary"]

    def _rate(rows, predicate):
        if not rows:
            return None
        return round(sum(1 for row in rows if predicate(row)) / len(rows), 4)

    metrics = {
        "cases": len(cases),
        "runs": len(results),
        "runtimes": list(runtimes),
        "pass_rate": _rate(results, lambda row: row["passed"]),
        "fail_closed_block_rate": _rate(
            fail_closed, lambda row: row["actual_status"] in ("blocked", "failed")
        ),
        "abstain_correct_rate": _rate(
            [row for row in abstain_cases if row["expected_status"] == "abstained"],
            lambda row: row["actual_status"] == "abstained",
        ),
        "research_only_leaks": sum(
            1 for row in boundary_cases if row["produced_finding"]
        ),
        "fact_plane_writes": _fact_plane_metrics(results),
        "runtime_divergence": _runtime_divergence(results),
    }
    metrics["all_hard_metrics_met"] = (
        metrics["pass_rate"] == 1.0
        and metrics["fail_closed_block_rate"] in (1.0, None)
        and metrics["abstain_correct_rate"] in (1.0, None)
        and metrics["research_only_leaks"] == 0
        and metrics["fact_plane_writes"]["completed_writes"] == 0
        and not metrics["runtime_divergence"]
    )

    return {
        "schema": REPORT_SCHEMA,
        "frozen_now": frozen_now,
        "scope": document.get("description"),
        "metrics": metrics,
        "failures": [row for row in results if not row["passed"]],
        "results": results,
    }


FACT_PLANE_REASON_CODES = ("forbidden_state_write", "fact_plane_directive")


def _fact_plane_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Count what the runs actually did, and say what the count is worth.

    A hardcoded zero read like a measured result. It is not one: these cases
    replay frozen turns, so the only thing demonstrated is that the contract
    blocks a *declared* write. Whether the process could write those paths at all
    is an operating-system question this harness never asks, and the report says
    so rather than implying a permission experiment took place.
    """

    attempts = [
        row for row in results
        if any(code in FACT_PLANE_REASON_CODES for code in row["actual_reason_codes"])
    ]
    return {
        "attempts_declared": len(attempts),
        "blocked_attempts": sum(1 for row in attempts if row["actual_status"] == "blocked"),
        "completed_writes": sum(1 for row in attempts if row["produced_finding"]),
        "guarantee_scope": "static_protocol_only",
        "measured_against": "frozen_turn_fixtures",
        "not_evidence_of": "operating_system_level_write_isolation",
    }


def _runtime_divergence(results: list[dict[str, Any]]) -> list[str]:
    """Case ids where two runtimes disagreed — a conformance break."""
    by_case: dict[str, set[tuple[str, tuple[str, ...]]]] = {}
    for row in results:
        by_case.setdefault(str(row["id"]), set()).add(
            (row["actual_status"], tuple(row["actual_reason_codes"]))
        )
    return sorted(case_id for case_id, outcomes in by_case.items() if len(outcomes) > 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=CASES_PATH)
    parser.add_argument("--runtime", action="append", default=None)
    parser.add_argument("--quiet", action="store_true", help="Print metrics only")
    args = parser.parse_args()

    report = evaluate(
        cases_path=args.cases,
        runtimes=tuple(args.runtime or ("hermes", "openclaw")),
    )
    payload = (
        {"schema": report["schema"], "metrics": report["metrics"],
         "failures": report["failures"]}
        if args.quiet
        else report
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report["metrics"]["all_hard_metrics_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
