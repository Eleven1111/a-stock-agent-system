"""Event-first projection orchestration with replay-safe checkpoints."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

from state_store import mutate_json, read_json


Projector = Callable[[Mapping[str, Any]], None]


def _sequence(event: Mapping[str, Any]) -> int:
    value = event.get("sequence")
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("event sequence must be an integer")
    sequence = int(value)
    if sequence < 1:
        raise ValueError("event sequence must be positive")
    return sequence


def _checkpoint(path: str) -> int:
    value = read_json(path, {})
    try:
        return max(0, int((value or {}).get("sequence") or 0))
    except (TypeError, ValueError):
        return 0


def _advance(path: str, event: Mapping[str, Any]) -> None:
    sequence = _sequence(event)
    def _mutate(current: Any) -> dict[str, Any]:
        current_sequence = int((current or {}).get("sequence") or 0)
        if sequence <= current_sequence:
            return dict(current or {})
        return {
            "schema": "projection_checkpoint_v1",
            "sequence": sequence,
            "event_id": event.get("event_id"),
        }

    mutate_json(path, _mutate, default={})


def advance_checkpoint(path: str, event: Mapping[str, Any] | None) -> None:
    """Acknowledge an already-applied canonical event monotonically."""
    if event:
        _advance(path, event)


def _apply_all(event: Mapping[str, Any], projectors: Sequence[Projector]) -> None:
    for projector in projectors:
        projector(event)


def append_project_checkpoint(
    event: Mapping[str, Any],
    *,
    append_event: Callable[[Mapping[str, Any]], Any],
    projectors: Sequence[Projector],
    checkpoint_file: str,
) -> dict[str, Any]:
    """Durably append first; never erase the event when a projection fails."""
    append_event(event)
    try:
        _apply_all(event, projectors)
        _advance(checkpoint_file, event)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return {
            "status": "replay_required",
            "allow_new_risk": False,
            "sequence": _sequence(event),
            "error_type": type(exc).__name__,
        }
    return {
        "status": "ok",
        "allow_new_risk": True,
        "sequence": _sequence(event),
    }


def replay_events(
    events: Iterable[Mapping[str, Any]],
    *,
    projectors: Sequence[Projector],
    checkpoint_file: str,
) -> dict[str, Any]:
    current = _checkpoint(checkpoint_file)
    applied = 0
    for event in sorted(events, key=_sequence):
        sequence = _sequence(event)
        if sequence <= current:
            continue
        try:
            _apply_all(event, projectors)
            _advance(checkpoint_file, event)
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            return {
                "status": "replay_required",
                "allow_new_risk": False,
                "applied": applied,
                "failed_sequence": sequence,
                "error_type": type(exc).__name__,
            }
        current = sequence
        applied += 1
    return {"status": "ok", "allow_new_risk": True, "applied": applied}


def _normalized(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Mapping):
        return {str(key): _normalized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    return value


def reconcile_projections(
    expected: Mapping[str, Any], actual: Mapping[str, Any]
) -> dict[str, Any]:
    fields = ("cash", "positions", "trades", "monitors")
    mismatches = [
        field
        for field in fields
        if _normalized(expected.get(field)) != _normalized(actual.get(field))
    ]
    return {
        "status": "projection_mismatch" if mismatches else "ok",
        "mismatches": mismatches,
        "allow_new_risk": not mismatches,
    }


def project_monitor_records(
    records: Iterable[Mapping[str, Any]], event: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Idempotently fold one canonical monitor event into registry records."""
    event_type = str(event.get("event_type") or "")
    if not event_type.startswith("monitor."):
        return [dict(record) for record in records]
    payload = dict(event.get("payload") or {})
    entry = dict(payload.get("entry") or payload)
    monitor_id = str(
        entry.get("id")
        or (event.get("links") or {}).get("monitor_id")
        or ""
    )
    if not monitor_id:
        raise ValueError("monitor projection requires monitor_id")
    entry["id"] = monitor_id
    items = [dict(record) for record in records]
    index = next(
        (offset for offset, record in enumerate(items) if record.get("id") == monitor_id),
        None,
    )
    if index is None:
        items.append(entry)
    else:
        items[index] = {**items[index], **entry}
    return items


def latest_portfolio_snapshot(
    events: Iterable[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Return the newest event-bound portfolio mutation projection."""
    latest: dict[str, Any] | None = None
    latest_sequence = 0
    for event in events:
        payload = event.get("payload") or {}
        snapshot = payload.get("portfolio_after") if isinstance(payload, Mapping) else None
        if not isinstance(snapshot, Mapping):
            continue
        sequence = _sequence(event)
        if sequence >= latest_sequence:
            latest = dict(snapshot)
            latest_sequence = sequence
    return latest
