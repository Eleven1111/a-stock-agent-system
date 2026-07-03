#!/usr/bin/env python3
"""Deterministic due-target planning for Serenity research (bus-backed).

``collect_targets``/``plan_refreshes``/``_due_reason`` decide *what is due*
for a Serenity deep-research refresh (portfolio, live recommendations, active
monitors, top candidates) and remain the single source of truth for that
judgement. ``plan_bus_refreshes`` is the current entry point: it reuses that
due-target logic and enqueues each due target as a ``serenity_refresh`` task
on the shared research bus (``research_bus.enqueue_task``), so the refresh
work shares one queue, one lease, and one budget with every other research
task kind. Idempotent dedup and cooldown come from the bus itself.

``plan_and_save``/``claim_next``/``complete_request``/``fail_request`` and
the standalone ``QUEUE_FILE`` below are the pre-bus queue and are DEPRECATED:
kept only because a deployed host may still have a historical backlog in
``serenity_refresh_queue.json`` to drain. New scheduling must go through
``plan_bus_refreshes``; do not write new callers against the deprecated
functions.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from typing import Any, Callable

from deep_research_cache import read_deep_research
import monitor_registry
from paths import data_file
import runtime_targets
from state_store import atomic_write_json, mutate_json, read_json


QUEUE_FILE = data_file("stock-triage", "serenity_refresh_queue.json")
ACTIVE_STATUSES = {"pending", "claimed"}
DEFAULT_LIMIT = 5


def load_queue(path: str | None = None) -> list[dict[str, Any]]:
    value = read_json(path or QUEUE_FILE, [])
    return value if isinstance(value, list) else []


def save_queue(
    requests: list[dict[str, Any]],
    path: str | None = None,
) -> None:
    atomic_write_json(path or QUEUE_FILE, requests)


def _due_reason(
    cache: dict[str, Any] | None,
    *,
    force: bool = False,
    refresh_after: str | None = None,
) -> str | None:
    if force:
        return "explicit_refresh"
    if not cache:
        return "missing_cache"
    if cache.get("stale"):
        return "stale_cache"
    if refresh_after and str(cache.get("asof") or "") < refresh_after:
        return "material_event"
    return None


def plan_refreshes(
    targets: list[dict[str, Any]],
    *,
    cache_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    asof: str | None = None,
    existing: list[dict[str, Any]] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    current = asof or date.today().isoformat()
    lookup = cache_lookup or read_deep_research
    requests = [dict(item) for item in (existing or [])]
    active_codes = {
        str(item.get("code") or "").zfill(6)
        for item in requests
        if item.get("status") in ACTIVE_STATUSES
    }
    deduped: dict[str, dict[str, Any]] = {}
    for raw in targets:
        code = str(raw.get("code") or "").zfill(6)
        if not code.strip("0"):
            continue
        item = deduped.setdefault(code, {
            "code": code,
            "name": raw.get("name") or code,
            "priority": int(raw.get("priority") or 0),
            "sources": [],
            "force": False,
            "refresh_after": None,
        })
        item["priority"] = max(item["priority"], int(raw.get("priority") or 0))
        item["force"] = bool(item["force"] or raw.get("force"))
        refresh_after = str(raw.get("refresh_after") or "")
        if refresh_after > str(item.get("refresh_after") or ""):
            item["refresh_after"] = refresh_after
        source = str(raw.get("source") or "unknown")
        if source not in item["sources"]:
            item["sources"].append(source)

    created = []
    for target in sorted(
        deduped.values(),
        key=lambda item: (-item["priority"], item["code"]),
    ):
        if len(created) >= max(0, limit):
            break
        code = target["code"]
        if code in active_codes:
            continue
        cache = lookup(code)
        reason = _due_reason(
            cache,
            force=target["force"],
            refresh_after=target.get("refresh_after"),
        )
        if not reason:
            continue
        request = {
            "id": f"serenity-{code}-{current}",
            "code": code,
            "name": target["name"],
            "status": "pending",
            "priority": target["priority"],
            "sources": target["sources"],
            "reason": reason,
            "refresh_after": target.get("refresh_after"),
            "requested_asof": current,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "attempts": 0,
        }
        requests.append(request)
        created.append(request)
        active_codes.add(code)
    return {
        "schema": "serenity_refresh_plan_v1",
        "asof": current,
        "created": len(created),
        "created_requests": created,
        "requests": requests,
    }


def collect_targets(
    *,
    portfolio: dict[str, Any] | None = None,
    recommendations: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    monitors: list[dict[str, Any]] | None = None,
    registry: list[dict[str, Any]] | None = None,
    candidate_limit: int = 5,
    asof: date | None = None,
) -> list[dict[str, Any]]:
    if portfolio is None:
        portfolio = read_json(
            data_file("stock-triage", "portfolio.json"),
            {"positions": []},
        )
    if recommendations is None:
        recommendations = read_json(
            data_file("stock-triage", "recommendations.json"),
            [],
        )
    if candidates is None:
        pool = read_json(
            data_file("stock-triage", "candidate_pool_latest.json"),
            {},
        )
        candidates = (pool or {}).get("candidates") or []
    if registry is None:
        registry = monitor_registry.load_registry() if monitors is None else []
    if monitors is None:
        monitors = [
            item
            for item in registry
            if item.get("kind") == "stock"
            and runtime_targets.is_active_entry(item, asof)
        ]

    targets = []
    cancelled = runtime_targets.cancelled_stock_codes(registry)

    def append_target(item: dict[str, Any]) -> None:
        code = runtime_targets.normalize_stock_code(item.get("code"))
        if code and code not in cancelled:
            item["code"] = code
            targets.append(item)

    for item in (portfolio or {}).get("positions") or []:
        append_target({
            "code": item.get("code"),
            "name": item.get("name"),
            "priority": 100,
            "source": "portfolio",
        })
    for item in recommendations or []:
        if item.get("outcome") not in {None, "pending"}:
            continue
        if item.get("action") not in {"buy", "add", "hold"}:
            continue
        announcement_scan = (
            (item.get("quality_report") or {}).get("announcement_scan") or {}
        )
        material_event = bool(
            announcement_scan.get("thesis_invalidation_hits")
            or announcement_scan.get("review_hits")
            or announcement_scan.get("clarification_hits")
            or announcement_scan.get("hard_risk_hits")
        )
        append_target({
            "code": item.get("code"),
            "name": item.get("name"),
            "priority": 90,
            "source": "recommendation",
            "refresh_after": item.get("date") if material_event else None,
        })
    for item in monitors or []:
        append_target({
            "code": item.get("key") or item.get("code"),
            "name": item.get("label") or item.get("name"),
            "priority": 80,
            "source": "monitor",
        })
    for index, item in enumerate((candidates or [])[:candidate_limit]):
        append_target({
            "code": item.get("code"),
            "name": item.get("name"),
            "priority": max(40, 70 - index),
            "source": "candidate_pool",
        })
    return targets


def _enqueue_bus_refresh(
    request: dict[str, Any],
    *,
    day: str,
    config: dict[str, Any],
    research_bus: Any,
) -> dict[str, Any]:
    subject = {"code": request["code"], "name": request.get("name")}
    reason = str(request.get("reason") or "serenity_due")
    outcome = research_bus.enqueue_task(
        "serenity_refresh",
        subject,
        reason=reason,
        trigger={
            "source": "serenity_refresh_queue.plan_bus_refreshes",
            "sources": request.get("sources"),
        },
        trading_date=day,
        priority=int(request.get("priority") or 0) or None,
        config=config,
    )
    entry = {
        "code": request["code"],
        "reason": reason,
        "enqueued": outcome.get("enqueued", False),
    }
    if outcome.get("enqueued"):
        entry["task_id"] = outcome["task"]["id"]
        research_bus.append_ledger_event({
            "event_type": "research.enqueued",
            "task_id": outcome["task"]["id"],
            "kind": "serenity_refresh",
            "reason": reason,
            "trading_date": day,
        })
    else:
        entry["skip_reason"] = outcome.get("reason")
    return entry


def plan_bus_refreshes(
    *,
    trading_date: str | None = None,
    limit: int = DEFAULT_LIMIT,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enqueue due Serenity targets as ``serenity_refresh`` research tasks.

    This is the current scheduling entry point (§6a): it reuses
    ``collect_targets``/``plan_refreshes`` to decide who is due, then calls
    ``research_bus.enqueue_task`` per due target instead of writing the
    deprecated standalone queue file. Bus dedup/cooldown makes repeated calls
    on the same trading day a no-op for targets already queued or recently
    completed.
    """
    import research_bus

    config = config or research_bus.load_config()
    day = str(trading_date or date.today().isoformat())[:10]
    targets = collect_targets(asof=date.fromisoformat(day))
    planned = plan_refreshes(targets, asof=day, existing=[], limit=limit)

    results = [
        _enqueue_bus_refresh(request, day=day, config=config, research_bus=research_bus)
        for request in planned["created_requests"]
    ]
    return {
        "schema": "serenity_bus_plan_v1",
        "asof": day,
        "scanned": len(planned["created_requests"]),
        "enqueued": len([item for item in results if item["enqueued"]]),
        "results": results,
    }


def plan_and_save(
    *,
    asof: str | None = None,
    limit: int = DEFAULT_LIMIT,
    path: str | None = None,
) -> dict[str, Any]:
    """DEPRECATED: writes the standalone queue file. Use plan_bus_refreshes.

    Kept only to drain a deployed host's historical backlog in
    ``serenity_refresh_queue.json``.
    """
    queue_file = path or QUEUE_FILE
    target_date = date.fromisoformat(str(asof)[:10]) if asof else None
    targets = collect_targets(asof=target_date)
    result: dict[str, Any] = {}

    def _plan(value: Any) -> list[dict[str, Any]]:
        current = list(value) if isinstance(value, list) else []
        planned = plan_refreshes(
            targets,
            asof=asof,
            existing=current,
            limit=limit,
        )
        result.update(planned)
        return planned["requests"]

    mutate_json(queue_file, _plan, [])
    return result


def pending_requests(path: str | None = None) -> list[dict[str, Any]]:
    return [
        item for item in load_queue(path)
        if item.get("status") in ACTIVE_STATUSES
    ]


def claim_next(
    worker: str,
    path: str | None = None,
    *,
    now: str | None = None,
    claim_ttl_minutes: int = 120,
) -> dict[str, Any] | None:
    """DEPRECATED: claims from the standalone queue file.

    New work is claimed through ``expert_runner.py next`` against the
    research bus. Kept only to drain a deployed host's historical backlog.
    """
    queue_file = path or QUEUE_FILE
    claimed: dict[str, Any] = {}
    current_time = datetime.fromisoformat(now) if now else datetime.now()

    def _claim(value: Any) -> list[dict[str, Any]]:
        items = list(value) if isinstance(value, list) else []
        expiry = current_time - timedelta(minutes=max(1, claim_ttl_minutes))
        for item in items:
            if item.get("status") != "claimed":
                continue
            try:
                claimed_at = datetime.fromisoformat(str(item.get("claimed_at")))
            except (TypeError, ValueError):
                claimed_at = datetime.min
            if claimed_at <= expiry:
                item.update({
                    "status": "pending",
                    "last_error": "claim lease expired",
                    "lease_expired_at": current_time.isoformat(timespec="seconds"),
                })
                item.pop("claimed_by", None)
                item.pop("claimed_at", None)
        pending = sorted(
            (item for item in items if item.get("status") == "pending"),
            key=lambda item: (-int(item.get("priority") or 0), item.get("created_at") or ""),
        )
        if not pending:
            return items
        item = pending[0]
        item.update({
            "status": "claimed",
            "claimed_by": worker,
            "claimed_at": current_time.isoformat(timespec="seconds"),
            "attempts": int(item.get("attempts") or 0) + 1,
        })
        claimed.update(item)
        return items

    mutate_json(queue_file, _claim, [])
    return claimed or None


def complete_request(
    request_id: str,
    path: str | None = None,
) -> dict[str, Any]:
    """DEPRECATED: completes a standalone-queue request.

    New work completes through ``expert_runner.py submit``, whose fail-closed
    fresh-cache check (see ``research_bus._validate_kind_specific``) mirrors
    the two checks below. Kept only to drain a historical backlog.
    """
    queue_file = path or QUEUE_FILE
    current = next(
        (item for item in load_queue(queue_file) if item.get("id") == request_id),
        None,
    )
    if not current:
        return {"ok": False, "error": "request not found"}
    cache = read_deep_research(
        str(current.get("code") or ""),
        today=current.get("requested_asof"),
    )
    if not cache:
        return {"ok": False, "error": "fresh deep-research cache not found"}
    if str(cache.get("asof") or "") < str(current.get("requested_asof") or ""):
        return {"ok": False, "error": "deep-research cache predates refresh request"}

    completed: dict[str, Any] = {}

    def _complete(value: Any) -> list[dict[str, Any]]:
        items = list(value) if isinstance(value, list) else []
        for item in items:
            if item.get("id") != request_id:
                continue
            item.update({
                "status": "completed",
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "cache_asof": cache.get("asof"),
                "report_path": cache.get("report_path"),
            })
            completed.update(item)
            break
        return items

    mutate_json(queue_file, _complete, [])
    return {"ok": bool(completed), "request": completed}


def fail_request(
    request_id: str,
    error: str,
    *,
    retry: bool = True,
    path: str | None = None,
) -> dict[str, Any]:
    """DEPRECATED: fails a standalone-queue request.

    New work fails through ``expert_runner.py fail``. Kept only to drain a
    historical backlog.
    """
    queue_file = path or QUEUE_FILE
    failed: dict[str, Any] = {}

    def _fail(value: Any) -> list[dict[str, Any]]:
        items = list(value) if isinstance(value, list) else []
        for item in items:
            if item.get("id") != request_id:
                continue
            item.update({
                "status": "pending" if retry else "failed",
                "last_error": error,
                "failed_at": datetime.now().isoformat(timespec="seconds"),
            })
            item.pop("claimed_by", None)
            item.pop("claimed_at", None)
            failed.update(item)
            break
        return items

    mutate_json(queue_file, _fail, [])
    return {"ok": bool(failed), "request": failed}


def main() -> int:
    parser = argparse.ArgumentParser(description="Serenity 深研刷新队列")
    sub = parser.add_subparsers(dest="command", required=True)
    plan_bus = sub.add_parser(
        "plan-bus", help="把到期目标入队 research bus（当前调度入口）",
    )
    plan_bus.add_argument("--trading-date")
    plan_bus.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    plan = sub.add_parser("plan", help="[deprecated] 写独立队列文件")
    plan.add_argument("--asof")
    plan.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    sub.add_parser("list")
    claim = sub.add_parser("claim")
    claim.add_argument("--worker", required=True)
    complete = sub.add_parser("complete")
    complete.add_argument("--id", required=True)
    fail = sub.add_parser("fail")
    fail.add_argument("--id", required=True)
    fail.add_argument("--error", required=True)
    fail.add_argument("--no-retry", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.command == "plan-bus":
        result = plan_bus_refreshes(
            trading_date=args.trading_date, limit=args.limit,
        )
    elif args.command == "plan":
        result = plan_and_save(asof=args.asof, limit=args.limit)
    elif args.command == "list":
        result = {"requests": load_queue(), "pending": pending_requests()}
    elif args.command == "claim":
        result = {"request": claim_next(args.worker)}
    elif args.command == "complete":
        result = complete_request(args.id)
    else:
        result = fail_request(args.id, args.error, retry=not args.no_retry)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
