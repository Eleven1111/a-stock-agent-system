#!/usr/bin/env python3
"""Build the 07:00 A-share morning note."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

from morning_note import build_morning_note, render_morning_note_markdown  # noqa: E402
from paths import cache_dir, data_file  # noqa: E402
from runtime_context import load_latest_artifact, make_batch_id  # noqa: E402
from state_store import read_json  # noqa: E402


def _payload_from_artifact(job_id: str) -> tuple[dict[str, Any] | None, str | None]:
    artifact = load_latest_artifact(job_id)
    if not artifact:
        return None, job_id
    try:
        parsed = json.loads(artifact.get("stdout") or "")
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        parsed.setdefault("job_id", job_id)
        return parsed, None
    return {"job_id": job_id, "summary": artifact.get("summary")}, None


def _first_available(job_ids: list[str]) -> tuple[dict[str, Any] | None, list[str]]:
    missing: list[str] = []
    for job_id in job_ids:
        payload, missing_id = _payload_from_artifact(job_id)
        if payload is not None:
            return payload, missing
        if missing_id:
            missing.append(missing_id)
    return None, missing


def run_note() -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    trading_date = os.environ.get("A_STOCK_TRADING_DATE") or now[:10]
    batch_id = os.environ.get("A_STOCK_BATCH_ID") or make_batch_id(trading_date)
    missing: list[str] = []

    global_context, global_missing = _first_available(["global-evening", "global-preopen"])
    missing.extend(global_missing)
    news_context, news_missing = _first_available(["news-monitor", "news-monitor-intraday", "official-policy-watch"])
    missing.extend(news_missing)
    company_context, company_missing = _first_available(["company-event-opportunity-scan"])
    missing.extend(company_missing)
    if company_context is None:
        company_context = read_json(data_file("company-event-opportunities", "latest.json"), {})
        if not company_context:
            missing.append("company-event-opportunities/latest.json")

    event_context = read_json(data_file("stock-triage", "event_calendar_latest.json"), {})
    if not event_context:
        missing.append("event_calendar_latest.json")
    portfolio = read_json(data_file("stock-triage", "portfolio.json"), {"positions": []})
    registry = read_json(data_file("stock-triage", "monitor_registry.json"), [])
    candidate_pool = read_json(data_file("stock-triage", "candidate_pool_latest.json"), {})
    behavioral = read_json(os.path.join(cache_dir("stock-triage"), "behavioral_finance_context.json"), {})
    if not behavioral:
        missing.append("behavioral_finance_context.json")

    note = build_morning_note(
        trading_date=trading_date,
        batch_id=batch_id,
        global_context=global_context or {},
        news_context=news_context or {},
        company_events_context=company_context or {},
        event_calendar_context=event_context or {},
        portfolio=portfolio if isinstance(portfolio, dict) else {},
        monitor_registry=registry if isinstance(registry, list) else [],
        candidate_pool=candidate_pool if isinstance(candidate_pool, dict) else {},
        behavioral_context=behavioral if isinstance(behavioral, dict) else {},
        missing_inputs=missing,
        generated_at=now,
    )
    note["markdown"] = render_morning_note_markdown(note)
    return note


def main() -> None:
    parser = argparse.ArgumentParser(description="Build morning note")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--json-lines-safe", action="store_true")
    args = parser.parse_args()
    note = run_note()
    if args.json:
        print(json.dumps(note, ensure_ascii=False, indent=2))
    elif args.json_lines_safe:
        print(note["markdown"])
    else:
        print(note["markdown"])


if __name__ == "__main__":
    main()
