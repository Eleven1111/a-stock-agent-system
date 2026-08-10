#!/usr/bin/env python3
"""Write validated research data and create bounded retrieval bundles."""

from __future__ import annotations

import argparse
import json
from typing import Any

import skills.common  # noqa: F401,E402 -- puts skills/common on sys.path

from derived_research_store import write_dataset  # noqa: E402
from research_retrieval import (  # noqa: E402
    load_documents,
    search,
    store_bundle,
    store_document,
)
from state_store import atomic_write_json  # noqa: E402


def _json_file(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _emit(value: Any, output: str | None) -> None:
    if output:
        atomic_write_json(output, value)
        return
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Governed write-back and point-in-time retrieval for research data."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    write = subparsers.add_parser("write-derived", help="Write a validated dataset.")
    write.add_argument("--dataset-id", required=True)
    write.add_argument("--records", required=True, help="JSON array file")
    write.add_argument("--lineage", required=True, help="JSON object file")
    write.add_argument("--validation", required=True, help="JSON object file")
    write.add_argument("--point-in-time-cutoff", required=True)
    write.add_argument("--available-at", required=True)
    write.add_argument("--store-dir")
    write.add_argument("--output")

    ingest = subparsers.add_parser("ingest-document", help="Seal one document.")
    ingest.add_argument("--document", required=True, help="JSON object file")
    ingest.add_argument("--store-dir")
    ingest.add_argument("--output")

    query = subparsers.add_parser("query", help="Create a retrieval bundle.")
    query.add_argument("--query", required=True)
    query.add_argument("--asof", required=True)
    query.add_argument("--allowed-scope", action="append", required=True)
    query.add_argument("--document-store-dir")
    query.add_argument("--bundle-store-dir")
    query.add_argument("--semantic-scores", help="Optional JSON object file")
    query.add_argument("--limit", type=int, default=10)
    query.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "write-derived":
        result = write_dataset(
            args.dataset_id,
            _json_file(args.records),
            lineage=_json_file(args.lineage),
            validation=_json_file(args.validation),
            point_in_time_cutoff=args.point_in_time_cutoff,
            available_at=args.available_at,
            store_dir=args.store_dir,
        )
    elif args.command == "ingest-document":
        result = store_document(
            _json_file(args.document),
            store_dir=args.store_dir,
        )
    else:
        semantic_scores = (
            _json_file(args.semantic_scores) if args.semantic_scores else None
        )
        bundle = search(
            load_documents(args.document_store_dir),
            args.query,
            asof=args.asof,
            allowed_scopes=args.allowed_scope,
            semantic_scores=semantic_scores,
            limit=args.limit,
        )
        result = store_bundle(bundle, store_dir=args.bundle_store_dir)
    _emit(result, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
