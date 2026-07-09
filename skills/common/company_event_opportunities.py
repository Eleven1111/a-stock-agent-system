"""Deterministic company event opportunity scan logic."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Mapping, Sequence

from company_event_schema import SCHEMA, classify_event, make_opportunity, normalize_code
from paths import cache_dir, skill_data_dir
from state_store import atomic_write_json, read_json


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def _event_text(item: Mapping[str, Any]) -> str:
    parts = [
        item.get("title"),
        item.get("summary"),
        item.get("content"),
        item.get("event_type"),
        item.get("type"),
    ]
    return " ".join(str(part or "") for part in parts)


def _extract_events(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    events: list[dict[str, Any]] = []
    for key in ("events", "items", "news", "alerts", "signals", "company_events", "catalysts"):
        value = payload.get(key)
        if isinstance(value, list):
            events.extend(item for item in value if isinstance(item, dict))
    return events


def _target_map(targets: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for target in targets or []:
        code = normalize_code(target.get("code") or target.get("key"))
        if not code:
            continue
        result[code] = {
            "code": code,
            "name": str(target.get("name") or target.get("label") or code),
            "source": str(target.get("source") or "runtime"),
        }
    return result


def scan_company_event_opportunities(
    *,
    targets: Sequence[Mapping[str, Any]] | None = None,
    source_payloads: Sequence[Mapping[str, Any]] | None = None,
    trading_date: str,
    batch_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Scan already-collected evidence for event opportunities.

    The function intentionally does not fetch external data. Missing valuation
    inputs remain ``None`` and are listed in risk flags.
    """
    generated_at = _now_iso(now)
    targets_by_code = _target_map(targets or [])
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    unmatched = 0

    for payload in source_payloads or []:
        source = str(payload.get("source") or payload.get("job_id") or "artifact")
        for item in _extract_events(payload):
            event_type = classify_event(_event_text(item))
            if not event_type:
                continue
            code = normalize_code(item.get("code") or item.get("symbol") or item.get("stock_code"))
            if not code:
                unmatched += 1
                continue
            if targets_by_code and code not in targets_by_code:
                continue
            grouped.setdefault((code, event_type), []).append({
                "title": str(item.get("title") or item.get("summary") or event_type),
                "url": item.get("url"),
                "published_at": item.get("published_at") or item.get("date") or item.get("time"),
                "source": item.get("source") or source,
            })

    opportunities: list[dict[str, Any]] = []
    for (code, event_type), evidence in sorted(grouped.items()):
        target = targets_by_code.get(code, {"code": code, "name": code})
        suggestion = "avoid" if event_type == "unlock_reduction" else "watch"
        opportunities.append(make_opportunity(
            code=code,
            name=target["name"],
            event_type=event_type,
            evidence=evidence[:5],
            suggestion=suggestion,
        ))

    missing_errors: list[str] = []
    unavailable: list[str] = []
    if not source_payloads:
        missing_errors.append("source_payloads_missing")
    if not targets:
        unavailable.append("runtime_targets_missing_or_empty")
    if unmatched:
        unavailable.append(f"unmatched_event_without_code:{unmatched}")

    status = "ready" if opportunities else "no_events"
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "trading_date": trading_date,
        "batch_id": batch_id,
        "status": status,
        "scope": {"universe": "runtime_targets", "event_types": ["all"]},
        "opportunities": opportunities,
        "summary": {
            "opportunity_count": len(opportunities),
            "risk_event_count": sum(1 for item in opportunities if item["suggestion"] == "avoid"),
            "target_count": len(targets or []),
        },
        "has_signal": bool(opportunities),
        "missing_errors": missing_errors,
        "unavailable": unavailable,
    }


def load_default_source_payloads() -> list[dict[str, Any]]:
    """Read local caches that may contain company event evidence."""
    payloads: list[dict[str, Any]] = []
    for path in (
        os.path.join(cache_dir("stock-triage"), "catalyst_context.json"),
        os.path.join(skill_data_dir("stock-triage"), "event_calendar_latest.json"),
        os.path.join(skill_data_dir("stock-triage"), "candidate_pool_latest.json"),
    ):
        data = read_json(path, None)
        if isinstance(data, dict):
            data.setdefault("source", os.path.basename(path))
            payloads.append(data)
    return payloads


def write_company_event_outputs(result: Mapping[str, Any]) -> dict[str, str]:
    data_dir = skill_data_dir("company-event-opportunities")
    history_dir = os.path.join(data_dir, "history")
    latest_path = os.path.join(data_dir, "latest.json")
    history_path = os.path.join(history_dir, f"{result.get('trading_date') or 'unknown'}.json")
    atomic_write_json(latest_path, dict(result))
    atomic_write_json(history_path, dict(result))
    return {"latest": latest_path, "history": history_path}
