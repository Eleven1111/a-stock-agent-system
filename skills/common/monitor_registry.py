"""Shared lifecycle registry for stock, sector, and theme monitoring."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from paths import data_file
from state_store import file_lock, mutate_json, read_json
import event_projection
import monitor_ledger
import signal_ledger


REGISTRY_FILE = data_file("stock-triage", "monitor_registry.json")
LEDGER_FILE = signal_ledger.LEDGER_FILE
MIRROR_LEDGER_FILE = monitor_ledger.LEDGER_FILE
CHECKPOINT_FILE = data_file(
    "stock-triage", "monitor_registry_projection_checkpoint.json"
)
_DEFAULT_REGISTRY_FILE = REGISTRY_FILE
_DEFAULT_MIRROR_LEDGER_FILE = MIRROR_LEDGER_FILE
_DEFAULT_CHECKPOINT_FILE = CHECKPOINT_FILE
VALID_KINDS = {"stock", "sector", "theme"}
PORTFOLIO_SOURCES = {"portfolio_buy", "portfolio_sync"}
MANUAL_SOURCE = "manual"


def _checkpoint_file() -> str:
    if (
        REGISTRY_FILE != _DEFAULT_REGISTRY_FILE
        and CHECKPOINT_FILE == _DEFAULT_CHECKPOINT_FILE
    ):
        return f"{REGISTRY_FILE}.checkpoint.json"
    return CHECKPOINT_FILE


def _mirror_ledger_file() -> str:
    if (
        REGISTRY_FILE != _DEFAULT_REGISTRY_FILE
        and MIRROR_LEDGER_FILE == _DEFAULT_MIRROR_LEDGER_FILE
    ):
        return f"{REGISTRY_FILE}.events.jsonl"
    return MIRROR_LEDGER_FILE


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


def _record_monitor_event(
    event_type: str,
    entry: Mapping[str, Any],
    *,
    mutation_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "entry": dict(entry),
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
    }
    canonical = signal_ledger.append_event(
        event_type,
        _ledger_links(entry),
        payload,
        idempotency_key=(
            f"{event_type}:{entry['id']}:{mutation_id or uuid.uuid4().hex}"
        ),
        ledger_file=LEDGER_FILE,
    )
    if canonical is None:
        raise RuntimeError("canonical monitor event was not appended")
    try:
        monitor_ledger.append_event(
            event_type,
            canonical.get("links") or {},
            {
                **payload,
                "canonical_event_id": canonical.get("event_id"),
                "canonical_sequence": canonical.get("sequence"),
            },
            occurred_at=canonical.get("occurred_at"),
            ledger_file=_mirror_ledger_file(),
        )
    except (OSError, TimeoutError):
        # Compatibility mirror failure cannot invalidate the canonical event.
        pass
    return canonical


def _project_monitor_event(event: Mapping[str, Any]) -> None:
    if not str(event.get("event_type") or "").startswith("monitor."):
        return

    def _mutate(records: Any) -> list[dict[str, Any]]:
        return event_projection.project_monitor_records(
            records if isinstance(records, list) else [], event
        )

    mutate_json(REGISTRY_FILE, _mutate, default=[])


# 账本重放校验是「进程启动时的完整性守卫」，不是「每次 mutation 的守卫」：
# 进程内对注册表与账本的每一次写入都走本模块的 _activate_locked /
# _cancel_locked —— 它们在同一把 file_lock 事务里先追加规范事件再投影，
# 不变式由构造保证。因此首次校验成功之后，同进程后续调用可以跳过重放。
#
# 边界（改动前后一致，不是本次引入的）：本缓存不防护「另一个进程并发改写
# 注册表文件或账本」。此前的实现虽然每次都重读账本，但同样没有跨进程事务，
# 所以跨进程并发下的保证与改动前完全相同。
#
# 缓存键取 (注册表, 账本, checkpoint) 三元组：任何把本模块指向另一份数据集
# 的调用方（测试用 monkeypatch 换 tmp_path 是典型场景）都会自动重新校验。
# 校验失败时不置位（下次仍要重查）；事务中途失败时主动失效（半提交的事务
# 可能让投影落后于账本，必须靠下次重放恢复）。
_VERIFIED_DATASET: tuple[str, str, str] | None = None


def _dataset_key() -> tuple[str, str, str]:
    return (REGISTRY_FILE, LEDGER_FILE, _checkpoint_file())


def reset_verification_cache() -> None:
    """Force the next registry access to replay and verify the ledger again."""
    global _VERIFIED_DATASET
    _VERIFIED_DATASET = None


def _recover_registry_projection(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    return event_projection.replay_events(
        events,
        projectors=[_project_monitor_event],
        checkpoint_file=_checkpoint_file(),
    )


def _registry_projection_matches_ledger(events: Iterable[Mapping[str, Any]]) -> bool:
    expected = event_projection.fold_monitor_records([], events)
    actual = read_json(REGISTRY_FILE, [])
    actual_records = actual if isinstance(actual, list) else []
    # Build lookup of actual records by id for subset check
    actual_by_id: dict[str, dict[str, Any]] = {}
    for item in actual_records:
        if isinstance(item, Mapping) and item.get("id"):
            actual_by_id[str(item["id"])] = dict(item)
    # Check all event-projected records exist in registry with matching core fields.
    # Registry may have extra entries (e.g. from candidate_discovery) and extra
    # fields (e.g. created_at, metadata) that events don't carry.
    essential_keys = ("id", "kind", "key", "label", "status", "source")
    for exp in expected:
        exp_id = str(exp.get("id") or "")
        actual_rec = actual_by_id.get(exp_id)
        if actual_rec is None:
            return False
        for k in essential_keys:
            if exp.get(k) != actual_rec.get(k):
                return False
    return True


def _recover_and_reconcile_registry() -> None:
    global _VERIFIED_DATASET
    dataset = _dataset_key()
    if _VERIFIED_DATASET == dataset:
        return
    events = signal_ledger.read_events(LEDGER_FILE)
    migrated = False
    if not any(str(event.get("event_type") or "").startswith("monitor.") for event in events):
        legacy = read_json(REGISTRY_FILE, [])
        for entry in legacy if isinstance(legacy, list) else []:
            if not isinstance(entry, Mapping) or not entry.get("id"):
                continue
            signal_ledger.append_event(
                "monitor.migrated",
                _ledger_links(entry),
                {"entry": dict(entry), "migration": "legacy_registry_v1"},
                idempotency_key=f"monitor.migrated:{entry['id']}",
                ledger_file=LEDGER_FILE,
            )
            migrated = True
    if migrated:
        # 迁移刚刚追加了事件，必须重读一次账本，否则重放与校验会漏掉它们。
        events = signal_ledger.read_events(LEDGER_FILE)
    recovery = _recover_registry_projection(events)
    if recovery.get("status") != "ok" or not _registry_projection_matches_ledger(events):
        raise RuntimeError("monitor registry projection mismatch")
    _VERIFIED_DATASET = dataset


def _commit_projection(mutator: Any, outcome: dict[str, Any]) -> None:
    """Apply one registry mutation, invalidating the verification cache on failure."""
    committed = False
    try:
        mutate_json(REGISTRY_FILE, mutator, [])
        event_projection.advance_checkpoint(
            _checkpoint_file(), outcome.get("canonical_event")
        )
        committed = True
    finally:
        if not committed:
            # 半提交的事务可能让投影落后于账本，进程内不变式不再成立。
            reset_verification_cache()
    outcome.pop("canonical_event", None)


def load_registry() -> list[dict[str, Any]]:
    _recover_and_reconcile_registry()
    value = read_json(REGISTRY_FILE, [])
    return value if isinstance(value, list) else []


def get_entry(kind: str, key: str) -> dict[str, Any] | None:
    entry_id = _entry_id(kind, key)
    return next((item for item in load_registry() if item.get("id") == entry_id), None)


def _activate_locked(
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
    mutation_id = uuid.uuid4().hex
    outcome: dict[str, Any] = {}
    _recover_and_reconcile_registry()

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
        projected = {**existing, **updates}
        event = _record_monitor_event(
            "monitor.activated", projected, mutation_id=mutation_id
        )
        existing.update(updates)
        outcome.update(
            changed=True,
            entry=dict(existing),
            event_recorded=True,
            canonical_event=event,
        )
        return items

    _commit_projection(_mut, outcome)
    return outcome


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
    with file_lock(f"{REGISTRY_FILE}.event-transaction", timeout=30):
        return _activate_locked(
            kind,
            key,
            label,
            source,
            expires_at,
            force,
            metadata,
            source_group,
            trading_date,
            batch_id,
        )


def _cancel_locked(
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
    mutation_id = uuid.uuid4().hex
    outcome: dict[str, Any] = {}
    _recover_and_reconcile_registry()

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
        updates = {
            "status": status or ("cancelled" if manual else "closed"),
            "reason": reason,
            "manual_cancelled": bool(manual),
            "updated_at": now,
            "metadata": entry_metadata,
        }
        projected = {**existing, **updates}
        event_type = (
            "monitor.cancelled"
            if manual
            else "monitor.closed"
            if projected.get("status") == "closed"
            else "monitor.deactivated"
        )
        event = _record_monitor_event(
            event_type, projected, mutation_id=mutation_id
        )
        existing.update(updates)
        outcome.update(
            changed=True,
            entry=dict(existing),
            event_recorded=True,
            canonical_event=event,
        )
        return items

    _commit_projection(_mut, outcome)
    return outcome


def cancel(
    kind: str,
    key: str,
    reason: str,
    manual: bool = True,
    status: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    with file_lock(f"{REGISTRY_FILE}.event-transaction", timeout=30):
        return _cancel_locked(kind, key, reason, manual, status, metadata)


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
