#!/usr/bin/env python3
"""Compute research-only committee calibration from explicit JSON inputs."""

from __future__ import annotations

import argparse
import json
import os
import site
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
site.addsitedir(ROOT)
site.addsitedir(os.path.join(ROOT, "skills", "common"))

from skills.common.expert_calibration import (  # noqa: E402
    CalibrationDataError,
    build_review_registry,
    compute_calibration,
    persist_review_registry,
)


def _rows(path: str) -> list[object]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("rows") or value.get("stances") or value.get("outcomes") or []
    if not isinstance(value, list):
        raise CalibrationDataError(f"calibration_input_not_a_list: path={path}")
    return list(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="research-only expert calibration")
    parser.add_argument("--stances", required=True)
    parser.add_argument("--outcomes", required=True)
    parser.add_argument("--output")
    parser.add_argument("--registry-output")
    parser.add_argument("--min-settled", type=int, default=20)
    parser.add_argument("--min-accuracy", type=float, default=0.5)
    args = parser.parse_args()
    report = compute_calibration(_rows(args.stances), _rows(args.outcomes))
    registry = build_review_registry(
        report, min_settled=args.min_settled, min_accuracy=args.min_accuracy,
    )
    if args.output:
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    if args.registry_output:
        Path(args.registry_output).write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    elif args.output:
        persist_review_registry(registry)
    print(json.dumps({"report": report, "registry": registry}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
