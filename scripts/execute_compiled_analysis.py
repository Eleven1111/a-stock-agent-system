#!/usr/bin/env python3
"""Execute and independently validate one dual-Agent compilation handoff."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import skills.common  # noqa: F401,E402 -- puts skills/common on sys.path

import dataset_contract  # noqa: E402
import deterministic_executor  # noqa: E402
from state_store import atomic_write_json  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


def _load(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compilation", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--validated-at", required=True)
    parser.add_argument("--catalog", default=str(ROOT / "config" / "dataset_catalog.json"))
    parser.add_argument("--workspace-root")
    parser.add_argument("--store-dir")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    compilation = _load(args.compilation)
    inputs = _load(args.inputs)
    catalog = dataset_contract.load_catalog(args.catalog)
    if not isinstance(compilation, dict) or not isinstance(inputs, dict):
        raise SystemExit("compilation and inputs must be JSON objects")
    result = deterministic_executor.execute_compilation(
        compilation,
        inputs,
        catalog=catalog,
        validated_at=args.validated_at,
        workspace_root=args.workspace_root,
        timeout_seconds=args.timeout_seconds,
    )
    stored = deterministic_executor.store_execution(
        result, store_dir=args.store_dir
    )
    payload = {
        "execution": result,
        "storage": {
            "created": stored["created"],
            "artifact_path": stored["artifact_path"],
            "execution_hash": result["execution_hash"],
        },
    }
    if args.output:
        atomic_write_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["status"] == "validated" else 2


if __name__ == "__main__":
    raise SystemExit(main())
