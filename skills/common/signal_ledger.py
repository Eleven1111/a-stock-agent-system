#!/usr/bin/env python3
"""
Append-only canonical event ledger for recommendation feedback.

The JSONL ledger is the source of truth. Legacy JSON arrays remain writable
compatibility projections for older callers.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import string
import uuid
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from paths import backup_home, data_file, hermes_home
from state_store import file_lock


LEDGER_FILE = data_file("stock-triage", "signal_ledger.jsonl")
SCHEMA = "signal_ledger_event_v2"
COMPATIBLE_SCHEMAS = {"signal_ledger_event_v1", SCHEMA}
SETTLEABLE_ACTIONS = {"buy", "add"}
TRADE_ACTIONS = {"buy", "add"}
EVIDENCE_WEIGHT_HINTS = {"primary", "supporting", "context"}
UNKNOWN_EVIDENCE_SOURCES = [
    {"source": "unknown", "artifact": "unknown", "weight_hint": "context"}
]
TAIL_CLOSE_PROVENANCE_FIELDS = (
    "decision_mode",
    "snapshot_id",
    "snapshot_hash",
    "config_hash",
    "code_version",
)
TAIL_CLOSE_EVENT_TYPES = {
    "research_signal": "tail_close.signal_created",
    "simulated_order": "tail_close.order_simulated",
    "simulated_fill": "tail_close.fill_simulated",
    "simulation_reconciliation": "tail_close.simulation_reconciled",
    "manual_reconciliation": "tail_close.manual_reconciled",
}
_TAIL_CLOSE_STAGE_BY_TYPE = {
    "tail_close.signal_created": "signal",
    "tail_close.order_simulated": "order",
    "tail_close.fill_simulated": "fill",
    "tail_close.simulation_reconciled": "reconciliation",
    "tail_close.manual_reconciled": "manual_reconciliation",
}
_REAL_EXECUTION_ID_FIELDS = {
    "broker_order_id",
    "broker_trade_id",
    "exchange_order_id",
    "exchange_trade_id",
    "live_order_id",
    "real_order_id",
    "real_trade_id",
}
_EXECUTION_MODE_FIELDS = {
    "account_mode",
    "execution_mode",
    "order_mode",
    "trade_mode",
}
_REAL_EXECUTION_MODES = {"broker", "live", "real", "production"}
_REAL_EXECUTION_BOOLEAN_FIELDS = {
    "broker_called",
    "live_execution",
    "real_execution",
}
_REAL_EXECUTION_COUNT_FIELDS = {
    "automatic_order_count",
    "broker_call_count",
}


class SignalLedgerCorruptionError(RuntimeError):
    """Raised when a canonical ledger or its mirror cannot be parsed safely."""

    def __init__(self, line_number: int, reason: str):
        super().__init__(
            f"signal ledger corruption at line {line_number}: {reason}"
        )
        self.line_number = line_number


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


def normalize_evidence_sources(value: Any) -> list[dict[str, Any]]:
    normalized = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, Mapping):
                continue
            source = str(item.get("source") or "").strip()
            if not source:
                continue
            weight_hint = str(item.get("weight_hint") or "context").strip()
            if weight_hint not in EVIDENCE_WEIGHT_HINTS:
                weight_hint = "context"
            artifact = item.get("artifact")
            normalized.append({
                "source": source,
                "artifact": artifact if artifact is not None else "unknown",
                "weight_hint": weight_hint,
            })
    return normalized or [dict(UNKNOWN_EVIDENCE_SOURCES[0])]


def _payload_with_evidence_sources(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if event_type in {"recommendation.created", "signal.opened"}:
        out["evidence_sources"] = normalize_evidence_sources(
            out.get("evidence_sources")
        )
    return out


def _normalize_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    event_type = str(raw["event_type"]).strip()
    if not event_type:
        raise ValueError("signal ledger event requires event_type")
    links = dict(raw.get("links") or {})
    if not links.get("correlation_id"):
        raise ValueError("signal ledger event requires correlation_id")
    payload = _payload_with_evidence_sources(
        event_type,
        dict(raw.get("payload") or {}),
    )
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
        "payload": payload,
    }


def _read_events_unlocked(ledger_file: str) -> list[dict[str, Any]]:
    if not os.path.exists(ledger_file):
        return []
    events = []
    with open(ledger_file, "rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            try:
                line = raw_line.decode("utf-8")
                value = json.loads(line)
            except UnicodeDecodeError as exc:
                raise SignalLedgerCorruptionError(
                    line_number,
                    "invalid UTF-8",
                ) from exc
            except json.JSONDecodeError as exc:
                raise SignalLedgerCorruptionError(
                    line_number,
                    "invalid JSON",
                ) from exc
            if not isinstance(value, dict):
                raise SignalLedgerCorruptionError(
                    line_number,
                    "event is not a JSON object",
                )
            if value.get("schema") not in COMPATIBLE_SCHEMAS:
                raise SignalLedgerCorruptionError(
                    line_number,
                    "unsupported or missing schema",
                )
            payload = value.get("payload") or {}
            if not isinstance(payload, Mapping):
                raise SignalLedgerCorruptionError(
                    line_number,
                    "payload is not a JSON object",
                )
            event_type = str(value.get("event_type") or "")
            value["payload"] = _payload_with_evidence_sources(
                event_type,
                payload,
            )
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
        existing_events = _read_events_unlocked(path)
        existing_by_id = {
            event.get("event_id"): event
            for event in existing_events
        }
        existing_ids = set(existing_by_id)
        last_sequence = max(
            [
                int(event.get("sequence") or index)
                for index, event in enumerate(existing_events, start=1)
            ],
            default=0,
        )
        appended = []
        for event in normalized:
            if event["event_id"] in existing_ids:
                existing = existing_by_id[event["event_id"]]
                if str(event["event_type"]).startswith("tail_close."):
                    existing_fact = {
                        key: existing.get(key)
                        for key in ("event_type", "links", "payload")
                    }
                    incoming_fact = {
                        key: event.get(key)
                        for key in ("event_type", "links", "payload")
                    }
                    if json.dumps(
                        existing_fact,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ) != json.dumps(
                        incoming_fact,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ):
                        raise ValueError(
                            "tail-close idempotency conflict for "
                            f"{event['event_id']}"
                        )
                continue
            last_sequence += 1
            event["sequence"] = last_sequence
            appended.append(event)
            existing_ids.add(event["event_id"])
            existing_by_id[event["event_id"]] = event
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
        "evidence_sources": normalize_evidence_sources(
            record.get("evidence_sources")
        ),
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


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip()
    return len(text) == 64 and all(
        character in string.hexdigits for character in text
    )


def _tail_close_provenance(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("provenance")
    if not isinstance(raw, Mapping):
        raise ValueError("tail-close event requires provenance")
    provenance = dict(raw)
    for field in TAIL_CLOSE_PROVENANCE_FIELDS:
        value = str(raw.get(field) or "").strip()
        if not value:
            raise ValueError(f"tail-close event requires provenance.{field}")
        provenance[field] = value
    if provenance["decision_mode"] not in {"live", "replay"}:
        raise ValueError("tail-close provenance.decision_mode must be live or replay")
    for field in ("snapshot_hash", "config_hash"):
        if not _is_sha256(provenance[field]):
            raise ValueError(
                f"tail-close provenance.{field} must be a SHA-256 digest"
            )
        provenance[field] = provenance[field].lower()
    return provenance


def _contains_real_execution_marker(value: Any) -> bool:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).strip().lower()
            if (
                key in _REAL_EXECUTION_ID_FIELDS
                and item is not None
                and item is not False
                and item != ""
            ):
                return True
            if (
                key in _EXECUTION_MODE_FIELDS
                and str(item or "").strip().lower() in _REAL_EXECUTION_MODES
            ):
                return True
            if key == "live_order_sent" and item is not False:
                return True
            if key in _REAL_EXECUTION_BOOLEAN_FIELDS and item is not False:
                return True
            if key in _REAL_EXECUTION_COUNT_FIELDS:
                try:
                    if int(item) != 0:
                        return True
                except (TypeError, ValueError):
                    return True
            if key == "live_weight":
                try:
                    if float(item) != 0.0:
                        return True
                except (TypeError, ValueError):
                    return True
            if _contains_real_execution_marker(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_real_execution_marker(item) for item in value)
    return False


def _tail_close_identity(
    kind: str,
    record: Mapping[str, Any],
    links: Mapping[str, Any],
) -> tuple[dict[str, Any], str, str, str]:
    if kind not in TAIL_CLOSE_EVENT_TYPES:
        raise ValueError(f"unsupported tail-close event kind: {kind}")
    if not isinstance(record, Mapping):
        raise ValueError("tail-close event record must be a mapping")
    if not isinstance(links, Mapping):
        raise ValueError("tail-close event links must be a mapping")
    normalized_links = dict(links)
    if not str(normalized_links.get("correlation_id") or "").strip():
        raise ValueError("tail-close event requires correlation_id")

    strategy_id = str(record.get("strategy_id") or "").strip()
    if not strategy_id:
        raise ValueError("tail-close event requires strategy_id")
    signal_date = str(
        record.get("signal_date")
        or record.get("trading_date")
        or record.get("date")
        or record.get("asof")
        or ""
    ).strip()
    if not signal_date:
        raise ValueError("tail-close event requires signal date")
    try:
        datetime.fromisoformat(signal_date.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("tail-close event signal date is invalid") from exc

    signal_anchor = str(
        record.get("signal_id")
        or normalized_links.get("signal_id")
        or record.get("code")
        or ""
    ).strip()
    if not signal_anchor:
        raise ValueError("tail-close event requires signal identity")
    if (
        record.get("signal_id")
        and normalized_links.get("signal_id")
        and str(record["signal_id"]) != str(normalized_links["signal_id"])
    ):
        raise ValueError("tail-close event signal_id conflicts with links")
    return normalized_links, strategy_id, signal_date, signal_anchor


def _validate_manual_reconciliation(record: Mapping[str, Any]) -> None:
    for field in (
        "pilot_gate_hash",
        "simulation_fill_hash",
        "evidence_hash",
    ):
        if not _is_sha256(record.get(field)):
            raise ValueError(f"tail-close manual reconciliation requires {field}")
    for field in ("human_approval_id", "human_approved_at"):
        if not str(record.get(field) or "").strip():
            raise ValueError(f"tail-close manual reconciliation requires {field}")
    try:
        approved_at = datetime.fromisoformat(
            str(record["human_approved_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError(
            "tail-close manual reconciliation human_approved_at invalid"
        ) from exc
    if approved_at.tzinfo is None or approved_at.utcoffset() is None:
        raise ValueError(
            "tail-close manual reconciliation human_approved_at timezone missing"
        )
    if isinstance(record.get("actual_filled_quantity"), bool):
        raise ValueError("tail-close manual reconciliation actual fill invalid")
    try:
        actual_quantity = int(record.get("actual_filled_quantity"))
        actual_price = float(record.get("actual_fill_price"))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "tail-close manual reconciliation actual fill invalid"
        ) from exc
    if (
        actual_quantity < 0
        or not math.isfinite(actual_price)
        or actual_price <= 0
    ):
        raise ValueError("tail-close manual reconciliation actual fill invalid")
    if record.get("external_broker_evidence_confirmed") is not True:
        raise ValueError(
            "tail-close manual reconciliation broker evidence unconfirmed"
        )


def _validate_simulation_reconciliation(record: Mapping[str, Any]) -> None:
    for field in ("decision_hash", "fill_hash"):
        if not _is_sha256(record.get(field)):
            raise ValueError(
                f"tail-close simulation reconciliation requires {field}"
            )


def _tail_close_payload(
    kind: str,
    record: Mapping[str, Any],
    strategy_id: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        **dict(record),
        "strategy_id": strategy_id,
        "provenance": provenance,
        "research_only": True,
        "live_policy_effect": "none",
        "live_order_sent": False,
    }
    if kind == "research_signal":
        payload.update({
            "execution_action": "none",
            "execution_mode": "research",
            "simulation": False,
        })
    else:
        payload.update({
            "execution_mode": "simulated",
            "simulation": True,
        })
    if kind == "manual_reconciliation":
        _validate_manual_reconciliation(record)
        payload.update({
            "reconciliation_mode": "manual_external_evidence",
            "execution_mode": "manual_external_reconciliation",
            "simulation": False,
            "system_ordering": "forbidden",
            "broker_confirmed": True,
        })
    elif kind == "simulation_reconciliation":
        _validate_simulation_reconciliation(record)
        payload.update({
            "reconciliation_mode": "automatic_simulation",
            "broker_confirmed": False,
        })
    return payload


def _tail_close_event(
    kind: str,
    record: Mapping[str, Any],
    links: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_links, strategy_id, signal_date, signal_anchor = (
        _tail_close_identity(kind, record, links)
    )
    provenance = _tail_close_provenance(record)
    if _contains_real_execution_marker(record):
        raise ValueError("tail-close simulation event contains real execution marker")
    if (
        kind not in {"research_signal", "manual_reconciliation"}
        and (
            record.get("simulation") is False
            or record.get("simulated") is False
        )
    ):
        raise ValueError("tail-close simulation event contains real execution marker")
    payload = _tail_close_payload(kind, record, strategy_id, provenance)
    idempotency_seed = "|".join([
        strategy_id,
        signal_date,
        signal_anchor,
        provenance["snapshot_id"],
        provenance["snapshot_hash"],
        provenance["config_hash"],
    ])
    event_type = TAIL_CLOSE_EVENT_TYPES[kind]
    return {
        "event_type": event_type,
        "links": normalized_links,
        "payload": payload,
        "idempotency_key": f"{event_type}:{_stable_id('tail', idempotency_seed)}",
    }


def research_signal_event(
    record: Mapping[str, Any],
    links: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an auditable research signal with no live-policy side effect."""
    return _tail_close_event("research_signal", record, links)


def simulated_order_event(
    record: Mapping[str, Any],
    links: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an explicitly simulated order event; real markers fail closed."""
    return _tail_close_event("simulated_order", record, links)


def simulated_fill_event(
    record: Mapping[str, Any],
    links: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an explicitly simulated fill event; real markers fail closed."""
    return _tail_close_event("simulated_fill", record, links)


def simulation_reconciliation_event(
    record: Mapping[str, Any],
    links: Mapping[str, Any],
) -> dict[str, Any]:
    """Close one simulated lifecycle without claiming a broker reconciliation."""
    return _tail_close_event("simulation_reconciliation", record, links)


def manual_reconciliation_event(
    record: Mapping[str, Any],
    links: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a manual reconciliation of simulation state, never broker state."""
    return _tail_close_event("manual_reconciliation", record, links)


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
            payload = _payload_with_evidence_sources(
                event_type,
                dict(event.get("payload") or {}),
            )
            records[signal_id] = {
                **payload,
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


def _fold_tail_close_event(
    event: Mapping[str, Any],
    records: dict[str, dict[str, Any]],
    order: list[str],
) -> None:
    event_type = str(event.get("event_type") or "")
    stage = _TAIL_CLOSE_STAGE_BY_TYPE.get(event_type)
    if stage is None:
        return
    links = dict(event.get("links") or {})
    signal_id = str(links.get("signal_id") or "")
    if not signal_id:
        return
    if signal_id not in records:
        order.append(signal_id)
        records[signal_id] = {
            "schema": "tail_close_lifecycle_projection_v1",
            "signal_id": signal_id,
            "correlation_id": links.get("correlation_id"),
            "strategy_id": None,
            "stages": {},
            "violations": [],
        }
    record = records[signal_id]
    if record["correlation_id"] != links.get("correlation_id"):
        record["violations"].append("correlation_id_mismatch")
    if stage in record["stages"]:
        record["violations"].append(f"duplicate_{stage}")
        return
    payload = dict(event.get("payload") or {})
    strategy_id = str(payload.get("strategy_id") or "")
    if record["strategy_id"] is None:
        record["strategy_id"] = strategy_id
    elif record["strategy_id"] != strategy_id:
        record["violations"].append("strategy_id_mismatch")
    record["stages"][stage] = {
        "event_id": event.get("event_id"),
        "sequence": event.get("sequence"),
        "payload": payload,
    }


def _validate_projected_reconciliation(
    stages: Mapping[str, Any],
    violations: list[str],
) -> None:
    fill = (stages.get("fill") or {}).get("payload") or {}
    reconciliation = (stages.get("reconciliation") or {}).get("payload") or {}
    if not reconciliation:
        return
    if reconciliation.get("fill_hash") != fill.get("fill_hash"):
        violations.append("fill_hash_mismatch")
    if not _is_sha256(reconciliation.get("decision_hash")):
        violations.append("decision_hash_invalid")
    if (
        reconciliation.get("reconciliation_mode") != "automatic_simulation"
        or reconciliation.get("broker_confirmed") is not False
    ):
        violations.append("simulation_reconciliation_mode_invalid")


def _validate_projected_manual_reconciliation(
    stages: Mapping[str, Any],
    violations: list[str],
) -> None:
    fill = (stages.get("fill") or {}).get("payload") or {}
    manual = (stages.get("manual_reconciliation") or {}).get("payload") or {}
    if not manual:
        return
    if manual.get("simulation_fill_hash") != fill.get("fill_hash"):
        violations.append("manual_fill_hash_mismatch")
    if (
        manual.get("reconciliation_mode") != "manual_external_evidence"
        or manual.get("broker_confirmed") is not True
    ):
        violations.append("manual_reconciliation_mode_invalid")
    for field in (
        "pilot_gate_hash",
        "simulation_fill_hash",
        "evidence_hash",
    ):
        if not _is_sha256(manual.get(field)):
            violations.append(f"manual_{field}_invalid")


def _finalize_tail_close_record(record: dict[str, Any]) -> None:
    stages = record["stages"]
    violations = record["violations"]
    for required in ("signal", "order", "fill", "reconciliation"):
        if required not in stages:
            violations.append(f"{required}_missing")
    _validate_projected_reconciliation(stages, violations)
    _validate_projected_manual_reconciliation(stages, violations)
    sequences = [
        int(stages[name]["sequence"])
        for name in ("signal", "order", "fill", "reconciliation")
        if name in stages and stages[name].get("sequence") is not None
    ]
    if sequences != sorted(sequences):
        violations.append("stage_order_invalid")
    record["violations"] = sorted(set(violations))
    record["complete"] = not record["violations"]


def project_tail_close_lifecycle(
    events: Optional[Iterable[Mapping[str, Any]]] = None,
    *,
    ledger_file: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Build a research-only tail-close audit view from the canonical ledger."""
    stream = list(events) if events is not None else read_events(ledger_file)
    records: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in stream:
        _fold_tail_close_event(event, records, order)
    for signal_id in order:
        _finalize_tail_close_record(records[signal_id])
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
