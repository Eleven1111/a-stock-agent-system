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
import sys
from datetime import datetime
from typing import Any, Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))
import delivery_output


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


def has_delivery_anomaly(digest: Dict[str, Any]) -> bool:
    return bool(digest.get("missing_context")) or digest.get("status") != "ready"


def delivery_summary(digest: Dict[str, Any]) -> str:
    return (
        f"{digest['kind']} {str(digest.get('generated_at') or '')[:10]}："
        f"上游{digest.get('context_count', 0)}项齐备，无缺失上下文。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Digest upstream Hermes cron artifacts")
    parser.add_argument("--kind", default=os.environ.get("HERMES_JOB_ID", "context-digest"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--delivery-job-id", help="启用该 cron job 的无异常摘要投递")
    args = parser.parse_args()

    digest = build_digest(args.kind, _load_context_from_env())
    if args.json:
        if args.delivery_job_id:
            summary_payload = {
                "schema": "delivery_summary_v1",
                "job_id": args.delivery_job_id,
                "status": digest.get("status"),
                "summary": delivery_summary(digest),
                "alerts": [],
            }
            print(delivery_output.maybe_summarize_json(
                digest,
                summary_payload,
                job_id=args.delivery_job_id,
                has_anomaly=has_delivery_anomaly(digest),
            ))
        else:
            print(json.dumps(digest, ensure_ascii=False, indent=2))
    else:
        report = format_digest(digest)
        if args.delivery_job_id:
            print(delivery_output.maybe_summarize_text(
                report,
                delivery_summary(digest),
                job_id=args.delivery_job_id,
                has_anomaly=has_delivery_anomaly(digest),
            ))
        else:
            print(report)


if __name__ == "__main__":
    main()
