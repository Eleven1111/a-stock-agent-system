#!/usr/bin/env python3
"""
Hermes context artifact digest.

This script consumes HERMES_CONTEXT_FROM from hermes_job_runner and emits a
small structured digest for downstream triage. It intentionally reads artifacts,
not the live user conversation.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Any, Dict, List


def _load_context_from_env() -> List[Dict[str, Any]]:
    raw = os.environ.get("HERMES_CONTEXT_FROM", "[]")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def build_digest(kind: str, context: List[Dict[str, Any]]) -> Dict[str, Any]:
    missing = [c["job_id"] for c in context if c.get("missing")]
    available = [c for c in context if not c.get("missing")]
    highlights = []
    for item in available:
        summary = item.get("summary") or {}
        highlights.append({
            "job_id": item.get("job_id"),
            "run_id": item.get("run_id"),
            "status": item.get("status"),
            "finished_at": item.get("finished_at"),
            "summary": summary,
            "artifact_path": item.get("artifact_path"),
        })
    return {
        "schema": "hermes_context_digest_v1",
        "kind": kind,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "partial" if missing else "ready",
        "missing_context": missing,
        "context_count": len(available),
        "highlights": highlights,
    }


def format_digest(digest: Dict[str, Any]) -> str:
    lines = [f"## {digest['kind']} context digest", f"status: {digest['status']}"]
    if digest["missing_context"]:
        lines.append("missing: " + ", ".join(digest["missing_context"]))
    for item in digest["highlights"]:
        lines.append(f"- {item['job_id']}: {item['status']} | {item.get('summary') or {}}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Digest upstream Hermes cron artifacts")
    parser.add_argument("--kind", default=os.environ.get("HERMES_JOB_ID", "context-digest"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    digest = build_digest(args.kind, _load_context_from_env())
    if args.json:
        print(json.dumps(digest, ensure_ascii=False, indent=2))
    else:
        print(format_digest(digest))


if __name__ == "__main__":
    main()
