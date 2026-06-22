#!/usr/bin/env python3
"""
Append-only canonical event ledger for recommendation feedback.

The JSONL ledger is the source of truth. Legacy JSON arrays remain writable
compatibility projections for older callers.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from paths import backup_home, data_file, hermes_home
from state_store import file_lock


LEDGER_FILE = data_file("stock-triage", "signal_ledger.jsonl")
SCHEMA = "signal_ledger_event_v1"
SETTLEABLE_ACTIONS = {"buy", "add"}
TRADE_ACTIONS = {"buy", "add"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def make_settlement_id(*parts: Any) -> str:
    return _stable_id("settle", "|".join(str(part) for part in parts))


def make_trade_execution_id(*parts: Any) -> str:
    seed = "|".join(str(part) for part in parts)
    return _stable_id("trade-exec", f"{seed}|{uuid.uuid4().hex}")


def make_links(
    recommendation_id: Optional[str] = None,
    *,
    correlation_id: Optional[str] = None,
    signal_id: Optional[str] = None,
    trade_id: Optional[str] = None,
    monitor_id: Optional[str] = None,
    settlement_id: Optional[str] = None,
    include_trade: bool = False,
) -> dict[str, Optional[str]]:
    """Build stable cross-entity IDs for one recommendation lifecycle."""
    seed = recommendation_id or signal_id or trade_id or uuid.uuid4().hex
    return {
        "correlation_id": correlation_id or _stable_id("corr", seed),
        "recommendation_id": recommendation_id,
        "signal_id": signal_id or (
            _stable_id("sig", recommendation_id) if recommendation_id else None
        ),
        "trade_id": trade_id or (
            _stable_id("trade", recommendation_id)
            if recommendation_id and include_trade
            else None
        ),
        "monitor_id": monitor_id,
        "settlement_id": settlement_id,
    }


def legacy_signal_links(record: Mapping[str, Any]) -> dict[str, Optional[str]]:
    """Give an old signal_history row deterministic IDs without rewriting it."""
    seed = "|".join([
        str(record.get("code") or ""),
        str(record.get("signal_date") or record.get("date") or ""),
        str(record.get("strategy_id") or record.get("strategy_type") or "default"),
        str(record.get("grade") or ""),
    ])
    signal_id = str(record.get("signal_id") or _stable_id("legacy-sig", seed))
    return make_links(
        recommendation_id=record.get("recommendation_id"),
        correlation_id=record.get("correlation_id") or _stable_id("corr", signal_id),
        signal_id=signal_id,
        trade_id=record.get("trade_id"),
        monitor_id=record.get("monitor_id"),
        settlement_id=record.get("settlement_id"),
    )


def _event_id(event_type: str, links: Mapping[str, Any], idempotency_key: Optional[str]) -> str:
    if idempotency_key:
        return _stable_id("evt", f"{event_type}|{idempotency_key}")
    anchor = (
        links.get("settlement_id")
        or links.get("signal_id")
        or links.get("recommendation_id")
        or links.get("correlation_id")
        or uuid.uuid4().hex
    )
    return _stable_id("evt", f"{event_type}|{anchor}")


def _normalize_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    event_type = str(raw["event_type"]).strip()
    links = dict(raw.get("links") or {})
    if not links.get("correlation_id"):
        raise ValueError("signal ledger event requires correlation_id")
    return {
        "schema": SCHEMA,
        "event_id": raw.get("event_id") or _event_id(
            event_type,
            links,
            raw.get("idempotency_key"),
        ),
        "event_type": event_type,
        "occurred_at": raw.get("occurred_at") or _now(),
        "links": links,
        "payload": dict(raw.get("payload") or {}),
    }


def _read_events_unlocked(ledger_file: str) -> list[dict[str, Any]]:
    if not os.path.exists(ledger_file):
        return []
    events = []
    with open(ledger_file, "r", encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("schema") == SCHEMA:
                events.append(value)
    return events


def _ledger_backup_path(ledger_file: str) -> str | None:
    root = os.path.abspath(os.path.expanduser(hermes_home()))
    path = os.path.abspath(os.path.expanduser(ledger_file))
    try:
        if os.path.commonpath([root, path]) != root:
            return None
    except ValueError:
        return None
    if os.path.basename(path) != "signal_ledger.jsonl":
        return None
    return os.path.join(backup_home(), os.path.relpath(path, root))


def _sync_ledger_backup_unlocked(ledger_file: str) -> None:
    backup = _ledger_backup_path(ledger_file)
    if backup is None:
        return
    events = _read_events_unlocked(ledger_file)
    try:
        with file_lock(backup):
            mirrored_ids = {
                event.get("event_id")
                for event in _read_events_unlocked(backup)
            }
            missing = [
                event for event in events
                if event.get("event_id") not in mirrored_ids
            ]
            if not missing:
                return
            os.makedirs(os.path.dirname(backup), mode=0o700, exist_ok=True)
            with open(backup, "a", encoding="utf-8") as handle:
                for event in missing:
                    handle.write(json.dumps(event, ensure_ascii=False, default=str))
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(backup, 0o600)
    except (OSError, TimeoutError):
        return


def _restore_ledger_unlocked(ledger_file: str) -> bool:
    backup = _ledger_backup_path(ledger_file)
    if backup is None or not os.path.exists(backup):
        return False
    try:
        with file_lock(backup):
            events = _read_events_unlocked(backup)
        if not events:
            return False
        os.makedirs(os.path.dirname(ledger_file), exist_ok=True)
        temporary = f"{ledger_file}.{os.getpid()}.restore.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False, default=str))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, ledger_file)
        return True
    except (OSError, TimeoutError):
        return False


def read_events(ledger_file: Optional[str] = None) -> list[dict[str, Any]]:
    path = ledger_file or LEDGER_FILE
    with file_lock(path):
        if not os.path.exists(path):
            _restore_ledger_unlocked(path)
        return _read_events_unlocked(path)


def sync_backup(ledger_file: Optional[str] = None) -> str | None:
    """Reconcile the canonical ledger mirror and return its path."""
    path = ledger_file or LEDGER_FILE
    with file_lock(path):
        if not os.path.exists(path):
            _restore_ledger_unlocked(path)
        if os.path.exists(path):
            _sync_ledger_backup_unlocked(path)
    return _ledger_backup_path(path)


def append_events(
    events: Iterable[Mapping[str, Any]],
    ledger_file: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Append missing events in one lock; repeated idempotency keys are no-ops."""
    path = ledger_file or LEDGER_FILE
    normalized = [_normalize_event(event) for event in events]
    if not normalized:
        return []
    with file_lock(path):
        if not os.path.exists(path):
            _restore_ledger_unlocked(path)
        existing_ids = {
            event.get("event_id")
            for event in _read_events_unlocked(path)
        }
        appended = []
        for event in normalized:
            if event["event_id"] in existing_ids:
                continue
            appended.append(event)
            existing_ids.add(event["event_id"])
        if not appended:
            return []
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            for event in appended:
                handle.write(json.dumps(event, ensure_ascii=False, default=str))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _sync_ledger_backup_unlocked(path)
        return appended


def append_event(
    event_type: str,
    links: Mapping[str, Any],
    payload: Optional[Mapping[str, Any]] = None,
    *,
    idempotency_key: Optional[str] = None,
    occurred_at: Optional[str] = None,
    ledger_file: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    appended = append_events(
        [{
            "event_type": event_type,
            "links": links,
            "payload": payload or {},
            "idempotency_key": idempotency_key,
            "occurred_at": occurred_at,
        }],
        ledger_file=ledger_file,
    )
    return appended[0] if appended else None


def signal_opened_event(
    record: Mapping[str, Any],
    links: Mapping[str, Any],
) -> dict[str, Any]:
    signal_id = links.get("signal_id")
    payload = {
        "code": str(record.get("code") or "").zfill(6),
        "name": record.get("name"),
        "grade": record.get("grade"),
        "score": record.get("score"),
        "signal_date": record.get("signal_date") or record.get("date"),
        "signal_price": record.get("signal_price") or record.get("entry_price"),
        "strategy_id": record.get("strategy_id") or "default",
        "action": record.get("action") or "buy",
        "source": record.get("source") or "recommendation",
        "strategy_attributions": list(record.get("strategy_attributions") or []),
        "social_attention": dict(record.get("social_attention") or {}),
        "selection_context": dict(record.get("selection_context") or {}),
    }
    return {
        "event_type": "signal.opened",
        "links": links,
        "payload": payload,
        "idempotency_key": f"signal.opened:{signal_id}",
    }


def settlement_event(
    record: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    settlement_id: Optional[str] = None,
    stage: Optional[str] = None,
) -> dict[str, Any]:
    links = legacy_signal_links(record)
    normalized_stage = str(stage or "").lower()
    sid = str(
        settlement_id
        or (
            _stable_id("settle", f"{links['signal_id']}|{normalized_stage}")
            if normalized_stage in {"t1", "t3"}
            else links.get("settlement_id") or _stable_id("settle", str(links["signal_id"]))
        )
    )
    links["settlement_id"] = sid
    payload = dict(result)
    if normalized_stage == "t1":
        payload.setdefault("settlement_status", "provisional")
        payload.setdefault("resolved", False)
    elif normalized_stage == "t3":
        payload.setdefault("settlement_status", "final")
        payload.setdefault("resolved", True)
    else:
        payload.setdefault("settlement_status", "final")
        payload.setdefault("resolved", True)
    payload.setdefault("resolved_at", _now())
    event_type = (
        f"signal.{normalized_stage}_settled"
        if normalized_stage in {"t1", "t3"}
        else "signal.settled"
    )
    return {
        "event_type": event_type,
        "links": links,
        "payload": payload,
        "idempotency_key": f"{event_type}:{sid}",
    }


def project_signals(
    events: Optional[Iterable[Mapping[str, Any]]] = None,
    *,
    ledger_file: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Fold signal.opened + signal.settled events into current signal records."""
    stream = list(events) if events is not None else read_events(ledger_file)
    records: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in stream:
        links = dict(event.get("links") or {})
        signal_id = links.get("signal_id")
        if not signal_id:
            continue
        event_type = event.get("event_type")
        if event_type == "signal.opened":
            if signal_id not in records:
                order.append(signal_id)
            records[signal_id] = {
                **dict(event.get("payload") or {}),
                **{key: value for key, value in links.items() if value is not None},
                "outcome": "pending",
                "settlement_status": "pending",
            }
        elif event_type in {
            "signal.settled",
            "signal.t1_settled",
            "signal.t3_settled",
        } and signal_id in records:
            records[signal_id].update(dict(event.get("payload") or {}))
            records[signal_id].update(
                {key: value for key, value in links.items() if value is not None}
            )
    return [records[signal_id] for signal_id in order]


def merge_legacy_signals(
    canonical: Iterable[Mapping[str, Any]],
    legacy: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Prefer canonical projections and retain old JSON rows not yet migrated."""
    merged = [dict(record) for record in canonical]
    seen_ids = {record.get("signal_id") for record in merged}
    seen_keys = {
        (
            str(record.get("code") or "").zfill(6),
            record.get("signal_date") or record.get("date"),
            record.get("strategy_id") or record.get("strategy_type") or "default",
        )
        for record in merged
    }
    for raw in legacy:
        record = dict(raw)
        links = legacy_signal_links(record)
        record.update({key: value for key, value in links.items() if value is not None})
        key = (
            str(record.get("code") or "").zfill(6),
            record.get("signal_date") or record.get("date"),
            record.get("strategy_id") or record.get("strategy_type") or "default",
        )
        if record.get("signal_id") in seen_ids or key in seen_keys:
            continue
        merged.append(record)
        seen_ids.add(record.get("signal_id"))
        seen_keys.add(key)
    return merged
