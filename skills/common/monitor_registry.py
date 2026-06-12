"""Shared lifecycle registry for stock, sector, and theme monitoring."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Mapping

from paths import data_file
from state_store import mutate_json, read_json
import signal_ledger


REGISTRY_FILE = data_file("stock-triage", "monitor_registry.json")
LEDGER_FILE = signal_ledger.LEDGER_FILE
VALID_KINDS = {"stock", "sector", "theme"}


def _today(value: date | str | None = None) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10]) if value else date.today()


def _key(kind: str, value: str) -> str:
    raw = str(value).strip()
    return raw.zfill(6) if kind == "stock" and raw.isdigit() else raw


def _entry_id(kind: str, key: str) -> str:
    return f"{kind}:{_key(kind, key)}"


def _ledger_links(entry: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(entry.get("metadata") or {})
    return signal_ledger.make_links(
        metadata.get("recommendation_id"),
        correlation_id=metadata.get("correlation_id"),
        signal_id=metadata.get("signal_id") or f"monitor:{entry['id']}",
        trade_id=metadata.get("trade_id"),
        monitor_id=entry["id"],
    )


def _record_monitor_event(event_type: str, entry: Mapping[str, Any]) -> None:
    signal_ledger.append_event(
        event_type,
        _ledger_links(entry),
        {
            "kind": entry.get("kind"),
            "key": entry.get("key"),
            "label": entry.get("label"),
            "status": entry.get("status"),
            "source": entry.get("source"),
            "reason": entry.get("reason"),
            "expires_at": entry.get("expires_at"),
            "manual_cancelled": entry.get("manual_cancelled", False),
        },
        idempotency_key=":".join([
            event_type,
            str(entry.get("id")),
            str(entry.get("updated_at")),
            str(entry.get("status")),
        ]),
        ledger_file=LEDGER_FILE,
    )


def load_registry() -> list[dict[str, Any]]:
    value = read_json(REGISTRY_FILE, [])
    return value if isinstance(value, list) else []


def get_entry(kind: str, key: str) -> dict[str, Any] | None:
    entry_id = _entry_id(kind, key)
    return next((item for item in load_registry() if item.get("id") == entry_id), None)


def activate(
    kind: str,
    key: str,
    label: str,
    source: str,
    expires_at: date | str | None = None,
    force: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if kind not in VALID_KINDS:
        raise ValueError(f"unsupported monitor kind: {kind}")
    normalized = _key(kind, key)
    entry_id = _entry_id(kind, normalized)
    now = datetime.now().isoformat(timespec="seconds")
    outcome: dict[str, Any] = {}

    def _mut(records: Any) -> list[dict[str, Any]]:
        items = list(records) if isinstance(records, list) else []
        existing = next((item for item in items if item.get("id") == entry_id), None)
        if existing and existing.get("manual_cancelled") and not force:
            outcome.update(changed=False, reason="manual_cancel_tombstone", entry=dict(existing))
            return items
        if existing is None:
            existing = {"id": entry_id, "kind": kind, "key": normalized, "created_at": now}
            items.append(existing)
        entry_metadata = dict((existing or {}).get("metadata") or {})
        entry_metadata.update(dict(metadata or {}))
        if source in {"portfolio_buy", "portfolio_sync"}:
            entry_metadata["position_linked"] = True
        existing.update({
            "label": label or normalized,
            "status": "active",
            "source": source,
            "updated_at": now,
            "expires_at": _today(expires_at).isoformat() if expires_at else None,
            "manual_cancelled": False,
            "metadata": entry_metadata,
        })
        outcome.update(changed=True, entry=dict(existing))
        return items

    mutate_json(REGISTRY_FILE, _mut, [])
    if outcome.get("changed") and outcome.get("entry"):
        _record_monitor_event("monitor.activated", outcome["entry"])
    return outcome


def cancel(
    kind: str,
    key: str,
    reason: str,
    manual: bool = True,
    status: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _key(kind, key)
    entry_id = _entry_id(kind, normalized)
    now = datetime.now().isoformat(timespec="seconds")
    outcome: dict[str, Any] = {}

    def _mut(records: Any) -> list[dict[str, Any]]:
        items = list(records) if isinstance(records, list) else []
        existing = next((item for item in items if item.get("id") == entry_id), None)
        if existing is None:
            existing = {
                "id": entry_id,
                "kind": kind,
                "key": normalized,
                "label": normalized,
                "created_at": now,
            }
            items.append(existing)
        entry_metadata = dict(existing.get("metadata") or {})
        entry_metadata.update(dict(metadata or {}))
        existing.update({
            "status": status or ("cancelled" if manual else "closed"),
            "reason": reason,
            "manual_cancelled": bool(manual),
            "updated_at": now,
            "metadata": entry_metadata,
        })
        outcome.update(changed=True, entry=dict(existing))
        return items

    mutate_json(REGISTRY_FILE, _mut, [])
    if outcome.get("changed") and outcome.get("entry"):
        event_type = (
            "monitor.cancelled"
            if manual
            else "monitor.closed"
            if outcome["entry"].get("status") == "closed"
            else "monitor.deactivated"
        )
        _record_monitor_event(event_type, outcome["entry"])
    return outcome


def deactivate_automatic(kind: str, key: str, reason: str) -> dict[str, Any]:
    existing = get_entry(kind, key)
    if not existing:
        return {"changed": False, "reason": "not_found"}
    if existing.get("manual_cancelled"):
        return {"changed": False, "reason": "manual_cancel_tombstone", "entry": existing}
    if existing.get("source") == "manual" or (existing.get("metadata") or {}).get("position_linked"):
        return {"changed": False, "reason": "protected_subscription", "entry": existing}
    return cancel(kind, key, reason=reason, manual=False, status="inactive")


def active_entries(kind: str | None = None, asof: date | str | None = None) -> list[dict[str, Any]]:
    current = _today(asof)
    result = []
    for item in load_registry():
        if item.get("status") != "active":
            continue
        if kind and item.get("kind") != kind:
            continue
        expires = item.get("expires_at")
        if expires and date.fromisoformat(str(expires)[:10]) < current:
            continue
        result.append(item)
    return result


def active_stock_map(asof: date | str | None = None) -> dict[str, str]:
    return {
        str(item["key"]): str(item.get("label") or item["key"])
        for item in active_entries("stock", asof)
    }


def sync_positions(
    positions: Iterable[Mapping[str, Any]],
    asof: date | str | None = None,
) -> dict[str, Any]:
    current_codes: set[str] = set()
    for position in positions:
        code = _key("stock", str(position.get("code") or ""))
        if not code:
            continue
        current_codes.add(code)
        activate(
            "stock",
            code,
            str(position.get("name") or code),
            source="portfolio_sync",
            force=False,
            metadata={"position_linked": True},
        )

    closed = []
    for item in active_entries("stock", asof):
        if not (item.get("metadata") or {}).get("position_linked"):
            continue
        if item["key"] not in current_codes:
            cancel("stock", item["key"], reason="position_closed", manual=False, status="closed")
            closed.append(item["key"])
    return {"active_positions": sorted(current_codes), "closed": sorted(closed)}
