#!/usr/bin/env python3
"""Execute one allowlisted analysis_plan_v1 from JSON artifacts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
import skills.common  # noqa: F401,E402  -- exposes skills/common modules

import analysis_plan  # noqa: E402
import dataset_contract  # noqa: E402
from paths import data_file  # noqa: E402


def _load(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, help="analysis_plan_v1 JSON")
    parser.add_argument("--inputs", required=True, help="execution input JSON")
    parser.add_argument(
        "--catalog",
        default=os.path.join(ROOT, "config", "dataset_catalog.json"),
    )
    parser.add_argument(
        "--cache-dir",
        default=data_file("research-committee", "analysis_cache"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = _load(args.plan)
        inputs = _load(args.inputs)
        catalog = dataset_contract.load_catalog(args.catalog)
        if not isinstance(plan, dict) or not isinstance(inputs, dict):
            raise analysis_plan.AnalysisPlanError("plan_and_inputs_must_be_objects")
        result = analysis_plan.execute_plan(
            plan,
            inputs,
            catalog=catalog,
            cache_dir=args.cache_dir,
        )
    except (
        OSError,
        json.JSONDecodeError,
        analysis_plan.AnalysisPlanError,
        dataset_contract.DatasetContractError,
    ) as exc:
        errors = list(getattr(exc, "errors", (str(exc),)))
        print(json.dumps({
            "schema": "analysis_run_v1",
            "status": "blocked",
            "research_only": True,
            "trading_action": "none",
            "errors": errors,
        }, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
