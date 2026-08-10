#!/usr/bin/env python3
"""Governed CLI for learning-candidate discovery, review, and offline export."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
import skills.common  # noqa: F401,E402

import learning_eval_factory  # noqa: E402
import learning_ledger  # noqa: E402
from paths import data_file  # noqa: E402


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", default=learning_ledger.default_ledger_file())
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan")
    scan.add_argument(
        "--consumer-runs",
        default=data_file("research-committee", "consumer_runs"),
    )

    status = subparsers.add_parser("status")
    status.add_argument("--json", action="store_true")

    review = subparsers.add_parser("review")
    review.add_argument("--case-id", required=True)
    review.add_argument("--decision", choices=("accepted", "rejected"), required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--benchmark", help="Reviewed benchmark JSON; required for accepted")

    export = subparsers.add_parser("export")
    export.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "scan":
            result = learning_eval_factory.discover_consumer_failures(
                args.consumer_runs, ledger_file=args.ledger, now=_now()
            )
        elif args.command == "status":
            result = learning_ledger.project_cases(args.ledger)
        elif args.command == "review":
            benchmark = None
            if args.benchmark:
                with open(args.benchmark, encoding="utf-8") as handle:
                    benchmark = json.load(handle)
            result = learning_ledger.review_case(
                args.case_id,
                decision=args.decision,
                reviewer=args.reviewer,
                benchmark=benchmark,
                now=_now(),
                ledger_file=args.ledger,
            )
        else:
            result = learning_eval_factory.materialize_eval_suite(args.ledger, args.output)
    except (
        OSError,
        json.JSONDecodeError,
        learning_ledger.LearningCaseError,
        learning_eval_factory.EvalFactoryError,
    ) as exc:
        print(json.dumps({"status": "blocked", "errors": list(getattr(exc, "errors", (str(exc),)))}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
