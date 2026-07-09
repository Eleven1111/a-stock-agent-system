#!/usr/bin/env python3
"""Deterministic research-task dispatcher for the research plane.

Runs as a plain cron command job (no model turn). It scans facts the DAG has
already produced — candidate pool, agent-state behavior risk, settled signal
outcomes — and enqueues bounded research tasks on the research bus. Silence
is the default: no trigger, no task, no token spend. Model turns only happen
later when Hermes/OpenClaw claims the queued work through expert_runner.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

import research_bus  # noqa: E402
from agent_state import load_agent_state  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import read_json  # noqa: E402

try:  # noqa: SIM105 - optional monitor side effect must never block dispatch
    import monitor_registry  # noqa: E402
except Exception:  # noqa: BLE001
    monitor_registry = None


def _norm_code(value: Any) -> str:
    code = str(value or "").strip()
    return code.zfill(6) if code.isdigit() and code else code


def _enqueue(
    kind: str,
    subject: dict[str, Any],
    *,
    reason: str,
    trading_date: str,
    config: dict[str, Any],
    force: bool = False,
) -> dict[str, Any]:
    result = research_bus.enqueue_task(
        kind,
        subject,
        reason=reason,
        trigger={"source": "research_dispatch", "reason": reason},
        trading_date=trading_date,
        force=force,
        config=config,
    )
    entry = {
        "kind": kind,
        "subject_key": research_bus.subject_key(subject),
        "enqueued": result.get("enqueued", False),
    }
    if result.get("enqueued"):
        entry["task_id"] = result["task"]["id"]
        research_bus.append_ledger_event({
            "event_type": "research.enqueued",
            "task_id": result["task"]["id"],
            "kind": kind,
            "reason": reason,
            "trading_date": trading_date,
        })
    else:
        entry["skip_reason"] = result.get("reason")
    return entry


def _load_dispatch_triggers() -> dict[str, Any]:
    """Load config/research_dispatch.yaml without adding a YAML dependency.

    The file is intentionally small and only needs nested trigger scalars.
    Unknown lines are ignored so a malformed optional config degrades to the
    research_bus defaults.
    """
    path = os.path.join(ROOT, "config", "research_dispatch.yaml")
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return {}
    data: dict[str, Any] = {}
    current: list[str] = []
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        key, sep, value = raw.strip().partition(":")
        if not sep:
            continue
        level = indent // 2
        current = current[:level]
        current.append(key.strip())
        cursor = data
        for part in current[:-1]:
            cursor = cursor.setdefault(part, {})
        text = value.strip()
        if not text:
            cursor.setdefault(current[-1], {})
            continue
        lowered = text.lower()
        if lowered in {"true", "false"}:
            parsed: Any = lowered == "true"
        else:
            try:
                parsed = float(text) if "." in text else int(text)
            except ValueError:
                parsed = text.strip("\"'")
        cursor[current[-1]] = parsed
    triggers = data.get("triggers")
    return triggers if isinstance(triggers, dict) else {}


def _merge_dispatch_config(config: dict[str, Any]) -> dict[str, Any]:
    triggers = _load_dispatch_triggers()
    if not triggers:
        return config
    merged = json.loads(json.dumps(config))
    target = merged.setdefault("triggers", {})
    for name, trigger in triggers.items():
        if isinstance(trigger, dict):
            base = target.get(name) if isinstance(target.get(name), dict) else {}
            target[name] = {**base, **trigger}
        else:
            target[name] = trigger
    return merged


def scan_candidate_trigger(
    config: dict[str, Any],
    trading_date: str,
) -> list[dict[str, Any]]:
    trigger = (config.get("triggers") or {}).get("candidate_deep_dive") or {}
    if not trigger.get("enabled"):
        return []
    pool = read_json(data_file("stock-triage", "candidate_pool_latest.json"), {})
    if not isinstance(pool, dict) or pool.get("status") != "ready":
        return []
    top_k = int(trigger.get("top_k") or 2)
    results = []
    for index, candidate in enumerate((pool.get("candidates") or [])[:top_k]):
        code = _norm_code((candidate or {}).get("code"))
        if not code:
            continue
        results.append(_enqueue(
            "candidate_deep_dive",
            {"code": code, "name": (candidate or {}).get("name")},
            reason=f"candidate_pool_rank_{index + 1}",
            trading_date=trading_date,
            config=config,
        ))
    return results


def scan_anomaly_trigger(
    config: dict[str, Any],
    trading_date: str,
    state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    trigger = (config.get("triggers") or {}).get("anomaly_review") or {}
    if not trigger.get("enabled") or not isinstance(state, dict):
        return []
    behavior = state.get("behavior_risk") or {}
    level = str(
        behavior.get("level") or behavior.get("risk_level") or "",
    ).lower()
    watched = {str(item).lower() for item in trigger.get("behavior_risk_levels") or []}
    if level not in watched:
        return []
    return [_enqueue(
        "anomaly_review",
        {"theme": f"behavior_risk_{level}"},
        reason=f"behavior_risk_level_{level}",
        trading_date=trading_date,
        config=config,
    )]


def _final_loss_pct(signal: dict[str, Any]) -> float | None:
    if str(signal.get("settlement_status") or "") != "final":
        return None
    for key in ("t3_return_pct", "final_return_pct", "return_pct", "t1_return_pct"):
        value = signal.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def scan_postmortem_trigger(
    config: dict[str, Any],
    trading_date: str,
    state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    trigger = (config.get("triggers") or {}).get("postmortem") or {}
    if not trigger.get("enabled") or not isinstance(state, dict):
        return []
    threshold = float(trigger.get("min_final_loss_pct") or -5.0)
    losers = []
    for signal in state.get("signals") or []:
        if not isinstance(signal, dict):
            continue
        loss = _final_loss_pct(signal)
        if loss is not None and loss <= threshold:
            losers.append((loss, signal))
    losers.sort(key=lambda item: item[0])
    results = []
    for loss, signal in losers[: int(trigger.get("max_per_day") or 1)]:
        code = _norm_code(signal.get("code"))
        if not code:
            continue
        results.append(_enqueue(
            "postmortem",
            {"code": code, "name": signal.get("name")},
            reason=f"final_loss_{loss:.1f}pct",
            trading_date=trading_date,
            config=config,
        ))
    return results


def _event_expected_value(event: dict[str, Any]) -> float | None:
    value = event.get("expected_value_pct")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _activate_high_risk_monitor(
    event: dict[str, Any],
    *,
    trading_date: str,
) -> dict[str, Any] | None:
    if monitor_registry is None:
        return {"changed": False, "reason": "monitor_registry_unavailable"}
    code = _norm_code(event.get("code") or event.get("stock_code"))
    if not code:
        return {"changed": False, "reason": "missing_code"}
    try:
        return monitor_registry.activate(
            "stock",
            code,
            str(event.get("name") or code),
            "company_event_opportunities",
            metadata={
                "reason_code": "company_event_high_risk_monitor",
                "event_type": event.get("event_type"),
                "risk_level": event.get("risk_level"),
                "expected_value_pct": event.get("expected_value_pct"),
            },
            source_group="research_dispatch",
            trading_date=trading_date,
        )
    except Exception as exc:  # noqa: BLE001 - monitor registration is best effort
        return {"changed": False, "reason": f"monitor_activate_failed:{exc}"}


def scan_event_trigger(
    config: dict[str, Any],
    trading_date: str,
) -> list[dict[str, Any]]:
    trigger = (config.get("triggers") or {}).get("event_review") or {}
    if not trigger.get("enabled"):
        return []
    payload = read_json(data_file("company-event-opportunities", "latest.json"), {})
    if not isinstance(payload, dict):
        return []
    ev_threshold = float(trigger.get("min_expected_value_pct") or 5.0)
    max_per_day = int(trigger.get("max_per_day") or 5)
    results: list[dict[str, Any]] = []
    for event in payload.get("opportunities") or payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        risk_high = str(event.get("risk_level") or "").lower() == "high"
        expected_value = _event_expected_value(event)
        positive_ev = expected_value is not None and expected_value > ev_threshold
        if not (risk_high or positive_ev):
            continue
        code = _norm_code(event.get("code") or event.get("stock_code"))
        if not code:
            continue
        if risk_high:
            monitor_result = _activate_high_risk_monitor(event, trading_date=trading_date)
        else:
            monitor_result = None
        entry = _enqueue(
            "event_review",
            {
                "code": code,
                "name": event.get("name"),
                "event_type": event.get("event_type"),
                "risk_level": event.get("risk_level"),
            },
            reason=(
                "company_event_high_risk"
                if risk_high else f"company_event_expected_value_{expected_value:.1f}pct"
            ),
            trading_date=trading_date,
            config=config,
        )
        if monitor_result is not None:
            entry["monitor_registry"] = monitor_result
        results.append(entry)
        if len(results) >= max_per_day:
            break
    return results


def dispatch(
    *,
    trading_date: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = _merge_dispatch_config(config or research_bus.load_config())
    state = load_agent_state()
    results = []
    results.extend(scan_candidate_trigger(config, trading_date))
    results.extend(scan_event_trigger(config, trading_date))
    results.extend(scan_anomaly_trigger(config, trading_date, state))
    results.extend(scan_postmortem_trigger(config, trading_date, state))
    enqueued = [entry for entry in results if entry.get("enqueued")]
    return {
        "schema": "research_dispatch_v1",
        "status": "ok",
        "trading_date": trading_date,
        "agent_state_available": state is not None,
        "scanned": len(results),
        "enqueued": len(enqueued),
        "tasks": [entry.get("task_id") for entry in enqueued],
        "results": results,
        "queue": research_bus.queue_summary()["by_status"],
        "has_signal": bool(enqueued),
    }


def manual_enqueue(args: argparse.Namespace) -> dict[str, Any]:
    config = research_bus.load_config()
    subject: dict[str, Any] = {}
    if args.code:
        subject["code"] = args.code
    if args.name:
        subject["name"] = args.name
    if args.theme:
        subject["theme"] = args.theme
    if not subject:
        raise SystemExit("manual enqueue requires --code or --theme")
    entry = _enqueue(
        args.kind,
        subject,
        reason=args.reason or "manual_request",
        trading_date=args.trading_date or date.today().isoformat(),
        config=config,
        force=args.force,
    )
    return {
        "schema": "research_dispatch_v1",
        "status": "ok",
        "mode": "manual",
        "results": [entry],
        "has_signal": bool(entry.get("enqueued")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="研究任务确定性调度器")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--trading-date")
    parser.add_argument("--kind", help="手动入队的任务类型（如 user_request）")
    parser.add_argument("--code")
    parser.add_argument("--name")
    parser.add_argument("--theme")
    parser.add_argument("--reason")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.kind:
        result = manual_enqueue(args)
    else:
        result = dispatch(
            trading_date=args.trading_date or date.today().isoformat(),
        )
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
