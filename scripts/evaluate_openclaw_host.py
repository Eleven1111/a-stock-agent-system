#!/usr/bin/env python3
"""Run the host-integration eval against a real, installed OpenClaw.

Separate from ``evaluate_agent_harness.py`` on purpose.  That one replays frozen
turns and is a *protocol* test; it proves the contract holds and nothing about a
model.  This one needs the installed host and real model turns, so on a machine
without OpenClaw it reports ``not_run`` with a reason rather than substituting a
fake model and calling the result a pass.

The metrics deliberately split apart things that get conflated:

* a citation that *resolves* versus a citation that actually *supports* the claim
* a technical failure versus a grounded abstention versus an unfounded one
* offering a judgement versus improving the pre-registered independent score
* account-level return versus the quality of a research answer

Twenty stratified tasks is an engineering-acceptance suggestion.  It is not a
sample threshold for financial validity and must never be reported as one.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CASES_PATH = os.path.join(ROOT, "evals", "openclaw_host", "cases.json")

REPORT_SCHEMA = "openclaw_host_eval_report_v1"
NOT_RUN = "not_run"

METRIC_SPLITS = (
    "citations_resolvable",
    "citations_supporting",
    "technical_failures",
    "grounded_abstentions",
    "unfounded_abstentions",
    "judgements_offered",
    "independent_score_improved",
)


def load_cases(path: str = CASES_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if document.get("schema") != "openclaw_host_eval_cases_v1":
        raise ValueError("unsupported host eval case schema")
    return document


def host_availability(openclaw: str = "openclaw") -> dict[str, Any]:
    """Whether an installed host is reachable, and what version it reports."""

    binary = shutil.which(openclaw)
    if not binary:
        return {"available": False, "reason": "openclaw_binary_not_found"}
    try:
        completed = subprocess.run(
            [openclaw, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": f"openclaw_version_failed:{exc}"}
    if completed.returncode != 0:
        return {
            "available": False,
            "reason": (completed.stderr or completed.stdout or "unknown").strip()[:200],
        }
    return {"available": True, "binary": binary, "version": completed.stdout.strip()[:200]}


def summarise(
    cases: Sequence[Mapping[str, Any]], observations: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Fold per-case observations into metrics that keep the splits apart."""

    counts = {name: 0 for name in METRIC_SPLITS}
    for row in observations:
        for name in METRIC_SPLITS:
            if row.get(name):
                counts[name] += 1
    by_stratum: dict[str, int] = {}
    for case in cases:
        stratum = str(case.get("stratum"))
        by_stratum[stratum] = by_stratum.get(stratum, 0) + 1
    observed = len(observations)
    return {
        "cases": len(cases),
        "observed": observed,
        "by_stratum": dict(sorted(by_stratum.items())),
        "counts": counts,
        "citation_support_gap": counts["citations_resolvable"] - counts["citations_supporting"],
        "abstention_split": {
            "grounded": counts["grounded_abstentions"],
            "unfounded": counts["unfounded_abstentions"],
            "technical_failure": counts["technical_failures"],
        },
        "judgement_without_score_improvement": (
            counts["judgements_offered"] - counts["independent_score_improved"]
        ),
        "scope": "engineering_integration_only",
        "not_a_claim_about": ["strategy_validity", "investment_performance"],
    }


def evaluate(
    *,
    cases_path: str = CASES_PATH,
    openclaw: str = "openclaw",
    observations: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    document = load_cases(cases_path)
    cases = document.get("cases") or []
    availability = host_availability(openclaw)
    base = {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "case_count": len(cases),
        "host": availability,
        "delivery_disabled": True,
        "delivery_note": "outbound sending is off for this eval; use a test workspace and state root",
    }
    if not availability.get("available"):
        # No fake model stands in for the real one. An unrun eval reports itself
        # as unrun; a substituted model would report a pass that means nothing.
        return {
            **base,
            "status": NOT_RUN,
            "reason": availability.get("reason"),
            "metrics": None,
            "runnable_entrypoint": "python scripts/evaluate_openclaw_host.py",
        }
    if observations is None:
        return {
            **base,
            "status": NOT_RUN,
            "reason": "no_observations_supplied",
            "metrics": None,
            "runnable_entrypoint": "python scripts/evaluate_openclaw_host.py --observations <file>",
        }
    return {**base, "status": "ok", "metrics": summarise(cases, observations)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=CASES_PATH)
    parser.add_argument("--openclaw", default="openclaw")
    parser.add_argument(
        "--observations", default=None,
        help="JSON file of per-case observations recorded from real host runs",
    )
    args = parser.parse_args()
    observations = None
    if args.observations:
        with open(args.observations, encoding="utf-8") as handle:
            observations = json.load(handle)
    report = evaluate(
        cases_path=args.cases, openclaw=args.openclaw, observations=observations
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
