"""Immutable, source-versioned snapshots shared by every agent runtime."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from paths import hermes_home
from state_store import atomic_write_json, read_json


SCHEMA = "market_snapshot_v1"
SOURCE_ADAPTER_VERSIONS = {
    "tencent": "tencent-adapter-v2",
    "tencent_kline": "tencent-kline-adapter-v2",
    "tencent_hk": "tencent-hk-adapter-v2",
    "serpapi": "serpapi-adapter-v1",
    "sina": "sina-adapter-v1",
    "eastmoney": "eastmoney-adapter-v1",
    "cninfo": "cninfo-adapter-v1",
    "yfinance": "yfinance-adapter-v1",
    "usgs": "usgs-adapter-v1",
    "gdacs": "gdacs-adapter-v1",
    "akshare": "akshare-adapter-v1",
    "baostock": "baostock-adapter-v1",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def snapshot_root() -> str:
    return os.path.join(hermes_home(), "market", "snapshots")


def _collect_provider_names(value: Any, names: set[str]) -> None:
    if isinstance(value, Mapping):
        provider = value.get("provider")
        if isinstance(provider, str) and provider:
            names.add(provider)
        source = value.get("source")
        if isinstance(source, str) and source in SOURCE_ADAPTER_VERSIONS:
            names.add(source)
        source_health = value.get("source_health")
        if isinstance(source_health, Mapping):
            names.update(str(key) for key in source_health)
        for child in value.values():
            _collect_provider_names(child, names)
    elif isinstance(value, list):
        for child in value:
            _collect_provider_names(child, names)


def infer_source_versions(payload: Any) -> dict[str, str]:
    names: set[str] = set()
    _collect_provider_names(payload, names)
    return {
        name: SOURCE_ADAPTER_VERSIONS.get(name, "external-source-v1")
        for name in sorted(names)
    }


def write_snapshot(
    dataset: str,
    payload: Any,
    *,
    trading_date: str,
    batch_id: str,
    producer: str,
    producer_version: Optional[str] = None,
    source_versions: Optional[Mapping[str, str]] = None,
    captured_at: Optional[str] = None,
) -> dict[str, Any]:
    """Write a content-addressed snapshot; identical input reuses the same file."""
    versions = dict(source_versions or infer_source_versions(payload))
    payload_hash = _hash(payload)
    identity = {
        "dataset": dataset,
        "trading_date": trading_date,
        "batch_id": batch_id,
        "producer": producer,
        "producer_version": producer_version or "unknown",
        "payload_hash": payload_hash,
        "source_versions": versions,
    }
    snapshot_id = f"snap-{_hash(identity)[:24]}"
    directory = os.path.join(snapshot_root(), trading_date, dataset)
    path = os.path.join(directory, f"{snapshot_id}.json")
    record = {
        "schema": SCHEMA,
        "snapshot_id": snapshot_id,
        "dataset": dataset,
        "trading_date": trading_date,
        "batch_id": batch_id,
        "producer": producer,
        "producer_version": producer_version or "unknown",
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "payload_schema": payload.get("schema") if isinstance(payload, Mapping) else None,
        "payload_hash": payload_hash,
        "source_versions": versions,
        "payload": payload,
        "snapshot_path": path,
    }
    existing = read_json(path, None) if os.path.exists(path) else None
    if isinstance(existing, dict):
        if existing.get("payload_hash") != payload_hash:
            raise ValueError(f"immutable snapshot collision: {snapshot_id}")
        return existing
    atomic_write_json(path, record)
    return record


def read_snapshot(snapshot: str | Mapping[str, Any]) -> dict[str, Any]:
    """Load and validate one immutable snapshot record."""
    path = (
        str(snapshot.get("snapshot_path"))
        if isinstance(snapshot, Mapping)
        else str(snapshot)
    )
    record = read_json(path, None)
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise ValueError(f"invalid market snapshot: {path}")
    if record.get("payload_hash") != _hash(record.get("payload")):
        raise ValueError(f"market snapshot payload hash mismatch: {path}")
    return record


def materialize_input_snapshot(
    dataset: str,
    payload: Any,
    *,
    trading_date: str,
    batch_id: str,
    producer: str,
    producer_version: Optional[str] = None,
    source_versions: Optional[Mapping[str, str]] = None,
    captured_at: Optional[str] = None,
) -> dict[str, Any]:
    """Persist raw inputs, then read them back for deterministic consumption."""
    written = write_snapshot(
        dataset,
        payload,
        trading_date=trading_date,
        batch_id=batch_id,
        producer=producer,
        producer_version=producer_version,
        source_versions=source_versions,
        captured_at=captured_at,
    )
    loaded = read_snapshot(written)
    loaded["consumed_from_snapshot"] = True
    return loaded


def compact_ref(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the lineage fields safe to embed in downstream state."""
    return {
        key: snapshot.get(key)
        for key in (
            "schema",
            "snapshot_id",
            "snapshot_path",
            "payload_hash",
            "source_versions",
            "producer",
            "producer_version",
            "captured_at",
            "consumed_from_snapshot",
        )
    }
