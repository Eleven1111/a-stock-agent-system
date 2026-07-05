"""Shared lifecycle registry for stock, sector, and theme monitoring."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Mapping

from paths import data_file
from state_store import mutate_json, read_json
import monitor_ledger
import signal_ledger


REGISTRY_FILE = data_file("stock-triage", "monitor_registry.json")
LEDGER_FILE = monitor_ledger.LEDGER_FILE
VALID_KINDS = {"stock", "sector", "theme"}
PORTFOLIO_SOURCES = {"portfolio_buy", "portfolio_sync"}
MANUAL_SOURCE = "manual"


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


def _entry_source_group(entry: Mapping[str, Any]) -> str:
    metadata = entry.get("metadata") or {}
    return str(entry.get("source_group") or metadata.get("source_group") or entry.get("source") or "")


def _protected_from_automatic_change(entry: Mapping[str, Any]) -> bool:
    metadata = entry.get("metadata") or {}
    return bool(
        entry.get("manual_cancelled")
        or entry.get("source") == MANUAL_SOURCE
        or entry.get("source") in PORTFOLIO_SOURCES
        or metadata.get("position_linked")
    )


def _resolved_source_group(
    source: str,
    source_group: str | None,
    existing: Mapping[str, Any] | None,
) -> str:
    if source_group:
        return source_group
    if source in PORTFOLIO_SOURCES:
        return "portfolio"
    if source == MANUAL_SOURCE:
        return "manual"
    return (_entry_source_group(existing or {}) or source).strip()


def _record_monitor_event(event_type: str, entry: Mapping[str, Any]) -> None:
    monitor_ledger.append_event(
        event_type,
        _ledger_links(entry),
        {
            "kind": entry.get("kind"),
            "key": entry.get("key"),
            "label": entry.get("label"),
            "status": entry.get("status"),
            "source": entry.get("source"),
            "source_group": entry.get("source_group"),
            "reason": entry.get("reason"),
            "expires_at": entry.get("expires_at"),
            "last_seen_trading_date": entry.get("last_seen_trading_date"),
            "last_seen_batch_id": entry.get("last_seen_batch_id"),
            "manual_cancelled": entry.get("manual_cancelled", False),
        },
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
    source_group: str | None = None,
    trading_date: date | str | None = None,
    batch_id: str | None = None,
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
        if source in PORTFOLIO_SOURCES:
            entry_metadata["position_linked"] = True
        updates = {
            "label": label or normalized,
            "status": "active",
            "source": source,
            "source_group": _resolved_source_group(source, source_group, existing),
            "updated_at": now,
            "expires_at": _today(expires_at).isoformat() if expires_at else None,
            "manual_cancelled": False,
            "metadata": entry_metadata,
        }
        if trading_date is not None:
            updates["last_seen_trading_date"] = _today(trading_date).isoformat()
        if batch_id is not None:
            updates["last_seen_batch_id"] = str(batch_id)
        existing.update(updates)
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
    if _protected_from_automatic_change(existing):
        return {"changed": False, "reason": "protected_subscription", "entry": existing}
    return cancel(kind, key, reason=reason, manual=False, status="inactive")


def _normalize_target(kind: str, target: Mapping[str, Any]) -> dict[str, Any]:
    raw_key = target.get("key") or target.get("code")
    if raw_key is None:
        raise ValueError("monitor target requires key or code")
    key = _key(kind, str(raw_key))
    label = str(target.get("label") or target.get("name") or key)
    return {
        "key": key,
        "label": label,
        "metadata": dict(target.get("metadata") or {}),
        "expires_at": target.get("expires_at"),
    }


def reconcile_automatic(
    kind: str,
    targets: Iterable[Mapping[str, Any]],
    *,
    source: str,
    source_group: str,
    trading_date: date | str,
    batch_id: str,
    expires_at: date | str | None = None,
    replace_source_groups: Iterable[str] | None = None,
    reason: str = "not_in_latest_observation_batch",
) -> dict[str, Any]:
    """Replace an automatic observation set while preserving manual and portfolio entries."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unsupported monitor kind: {kind}")
    current_targets = {
        item["key"]: item
        for item in (_normalize_target(kind, target) for target in targets)
    }
    activated: list[str] = []
    skipped: dict[str, str] = {}
    for key, target in current_targets.items():
        outcome = activate(
            kind,
            key,
            target["label"],
            source=source,
            expires_at=target["expires_at"] or expires_at,
            metadata=target["metadata"],
            source_group=source_group,
            trading_date=trading_date,
            batch_id=batch_id,
        )
        if outcome.get("changed"):
            activated.append(key)
        else:
            skipped[key] = str(outcome.get("reason") or "skipped")

    scope = set(replace_source_groups or [source_group])
    scope.add(source_group)
    desired_keys = set(current_targets)
    deactivated: list[str] = []
    for item in load_registry():
        if item.get("status") != "active":
            continue
        if item.get("kind") != kind:
            continue
        key = str(item.get("key") or "")
        if key in desired_keys:
            continue
        if _entry_source_group(item) not in scope:
            continue
        if _protected_from_automatic_change(item):
            continue
        outcome = cancel(
            kind,
            key,
            reason=reason,
            manual=False,
            status="inactive",
            metadata={
                "deactivated_by_batch_id": str(batch_id),
                "deactivated_by_trading_date": _today(trading_date).isoformat(),
                "previous_source_group": _entry_source_group(item),
            },
        )
        if outcome.get("changed"):
            deactivated.append(key)

    return {
        "kind": kind,
        "source": source,
        "source_group": source_group,
        "batch_id": str(batch_id),
        "trading_date": _today(trading_date).isoformat(),
        "activated": activated,
        "deactivated": deactivated,
        "skipped": skipped,
    }


def gc_expired(
    asof: date | str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """Persistently deactivate expired automatic monitor entries."""
    current = _today(asof)
    expired: list[str] = []
    for item in load_registry():
        if item.get("status") != "active":
            continue
        if kind and item.get("kind") != kind:
            continue
        expires = item.get("expires_at")
        if not expires or date.fromisoformat(str(expires)[:10]) >= current:
            continue
        if _protected_from_automatic_change(item):
            continue
        outcome = cancel(
            str(item.get("kind")),
            str(item.get("key")),
            reason="expired",
            manual=False,
            status="inactive",
            metadata={
                "expired_asof": current.isoformat(),
                "previous_source_group": _entry_source_group(item),
            },
        )
        if outcome.get("changed"):
            expired.append(str(item.get("id")))
    return {"asof": current.isoformat(), "expired": expired}


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
    for item in load_registry():
        if item.get("status") != "active":
            continue
        if item.get("kind") != "stock":
            continue
        if not (item.get("metadata") or {}).get("position_linked"):
            continue
        if item["key"] not in current_codes:
            cancel("stock", item["key"], reason="position_closed", manual=False, status="closed")
            closed.append(item["key"])
    return {"active_positions": sorted(current_codes), "closed": sorted(closed)}
