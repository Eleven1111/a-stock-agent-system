"""Immutable, source-versioned snapshots shared by every agent runtime."""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from paths import hermes_home
from state_store import atomic_write_json, read_json


SCHEMA = "market_snapshot_v1"
PIT_STAGE_SCHEMA = "pit_stage_contract_v1"
DECISION_MODES = {"live", "replay"}
SOURCE_ADAPTER_VERSIONS = {
    "tencent": "tencent-adapter-v2",
    "tencent_kline": "tencent-kline-adapter-v2",
    "tencent_hk": "tencent-hk-adapter-v2",
    "serper": "serper-adapter-v1",
    "sina": "sina-adapter-v1",
    "eastmoney": "eastmoney-intelligence-v2",
    "cninfo": "cninfo-adapter-v1",
    "yfinance": "yfinance-adapter-v1",
    "usgs": "usgs-adapter-v1",
    "gdacs": "gdacs-adapter-v1",
    "akshare": "akshare-adapter-v1",
    "baostock": "baostock-adapter-v1",
    "eastmoney_attention": "eastmoney-attention-v1",
    "xueqiu_attention": "xueqiu-attention-v1",
    "baidu_attention": "baidu-attention-v1",
}


class PointInTimeViolation(ValueError):
    """Raised when evidence is unavailable at the bound decision stage."""


def build_stage_policy(
    *,
    stage: str,
    cutoff_time: str,
    timezone_name: str,
    publication_delay_seconds: int = 0,
) -> dict[str, Any]:
    """Build the versioned stage/cutoff policy used by replay and live runs."""
    if not stage:
        raise ValueError("stage is required")
    datetime_time.fromisoformat(cutoff_time)
    ZoneInfo(timezone_name)
    if publication_delay_seconds < 0:
        raise ValueError("publication_delay_seconds must be non-negative")
    return {
        "schema": PIT_STAGE_SCHEMA,
        "stage": stage,
        "cutoff_time": cutoff_time,
        "timezone": timezone_name,
        "publication_delay_seconds": int(publication_delay_seconds),
    }


def _aware_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise PointInTimeViolation(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PointInTimeViolation(f"{field}_timezone_missing")
    return parsed


def validate_point_in_time(
    *,
    event_asof: str,
    evidence_time: str,
    captured_at: str,
    decision_mode: str,
    stage_policy: Mapping[str, Any],
    event_time: Optional[str] = None,
    available_time: Optional[str] = None,
    watermark: Optional[Mapping[str, Any]] = None,
    sealed_at: Optional[str] = None,
    max_clock_drift_seconds: Optional[float] = None,
) -> dict[str, Any]:
    """Validate evidence availability against a versioned decision-stage cutoff."""
    if stage_policy.get("schema") != PIT_STAGE_SCHEMA:
        raise PointInTimeViolation("stage_policy_invalid")
    if decision_mode not in DECISION_MODES:
        raise PointInTimeViolation("decision_mode_invalid")
    try:
        event_day = date.fromisoformat(str(event_asof))
        zone = ZoneInfo(str(stage_policy["timezone"]))
        cutoff_clock = datetime_time.fromisoformat(str(stage_policy["cutoff_time"]))
        delay = int(stage_policy.get("publication_delay_seconds") or 0)
    except (KeyError, TypeError, ValueError) as exc:
        raise PointInTimeViolation("stage_policy_invalid") from exc
    cutoff = datetime.combine(event_day, cutoff_clock, tzinfo=zone)
    evidence = _aware_datetime(evidence_time, "evidence_time")
    captured = _aware_datetime(captured_at, "captured_at")
    expected_offset = cutoff.utcoffset()
    if evidence.utcoffset() != expected_offset or captured.utcoffset() != expected_offset:
        raise PointInTimeViolation("timezone_mismatch")
    if captured > cutoff:
        raise PointInTimeViolation("capture_after_cutoff")
    available_evidence_cutoff = cutoff - timedelta(seconds=delay)
    if evidence > available_evidence_cutoff or evidence > captured:
        raise PointInTimeViolation("future_evidence")
    result = {
        "schema": PIT_STAGE_SCHEMA,
        "decision_mode": decision_mode,
        "event_asof": event_day.isoformat(),
        "evidence_time": evidence.isoformat(),
        "captured_at": captured.isoformat(),
        "stage_policy": dict(stage_policy),
        "available_evidence_cutoff": available_evidence_cutoff.isoformat(),
    }
    strict_fields = (
        event_time,
        available_time,
        watermark,
        sealed_at,
        max_clock_drift_seconds,
    )
    if not any(value is not None for value in strict_fields):
        return result
    if not all(value is not None for value in strict_fields):
        raise PointInTimeViolation("strict_point_in_time_contract_incomplete")
    if not isinstance(watermark, Mapping):
        raise PointInTimeViolation("watermark_invalid")
    try:
        max_drift = float(max_clock_drift_seconds)
    except (TypeError, ValueError) as exc:
        raise PointInTimeViolation("clock_drift_limit_invalid") from exc
    if isinstance(max_clock_drift_seconds, bool) or not math.isfinite(max_drift) or max_drift < 0:
        raise PointInTimeViolation("clock_drift_limit_invalid")

    event = _aware_datetime(str(event_time), "event_time")
    available = _aware_datetime(str(available_time), "available_time")
    sealed = _aware_datetime(str(sealed_at), "sealed_at")
    try:
        coverage = _aware_datetime(
            str(watermark["coverage_asof"]),
            "watermark_coverage_asof",
        )
        provider_published = _aware_datetime(
            str(watermark["provider_published_at"]),
            "watermark_provider_published_at",
        )
    except KeyError as exc:
        raise PointInTimeViolation("watermark_invalid") from exc
    if watermark.get("complete") is not True:
        raise PointInTimeViolation("watermark_incomplete")

    strict_times = (event, available, coverage, provider_published, sealed)
    if any(value.utcoffset() != expected_offset for value in strict_times):
        raise PointInTimeViolation("timezone_mismatch")
    if event > cutoff:
        raise PointInTimeViolation("event_after_cutoff")
    if available > cutoff:
        raise PointInTimeViolation("availability_after_cutoff")
    if coverage > cutoff or provider_published > cutoff:
        raise PointInTimeViolation("watermark_after_cutoff")
    if sealed > cutoff:
        raise PointInTimeViolation("seal_after_cutoff")
    if abs((available - event).total_seconds()) > max_drift:
        raise PointInTimeViolation("clock_drift_exceeded")
    if coverage > event:
        raise PointInTimeViolation("watermark_coverage_after_event")
    if provider_published > available:
        raise PointInTimeViolation("watermark_publication_after_availability")

    normalized_watermark = dict(watermark)
    normalized_watermark.update(
        {
            "coverage_asof": coverage.isoformat(),
            "provider_published_at": provider_published.isoformat(),
            "complete": True,
        }
    )
    result.update(
        {
            "event_time": event.isoformat(),
            "available_time": available.isoformat(),
            "watermark": normalized_watermark,
            "sealed_at": sealed.isoformat(),
            "max_clock_drift_seconds": max_drift,
        }
    )
    return result


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _seal_hash(payload_hash: str, point_in_time: Mapping[str, Any]) -> str:
    strict_point_in_time = {
        key: value
        for key, value in point_in_time.items()
        if key not in {"watermark", "sealed_at", "seal_hash"}
    }
    return _hash(
        {
            "payload_hash": payload_hash,
            "strict_point_in_time": strict_point_in_time,
            "watermark": point_in_time.get("watermark"),
            "sealed_at": point_in_time.get("sealed_at"),
        }
    )


def _snapshot_id_from_record(record: Mapping[str, Any]) -> str:
    identity = {
        key: record.get(key)
        for key in (
            "dataset",
            "trading_date",
            "batch_id",
            "producer",
            "producer_version",
            "payload_hash",
            "source_versions",
            "point_in_time",
        )
    }
    return f"snap-{_hash(identity)[:24]}"


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
    event_asof: Optional[str] = None,
    evidence_time: Optional[str] = None,
    decision_mode: Optional[str] = None,
    stage_policy: Optional[Mapping[str, Any]] = None,
    event_time: Optional[str] = None,
    available_time: Optional[str] = None,
    watermark: Optional[Mapping[str, Any]] = None,
    sealed_at: Optional[str] = None,
    max_clock_drift_seconds: Optional[float] = None,
) -> dict[str, Any]:
    """Write a content-addressed snapshot; identical input reuses the same file."""
    captured = captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    strict_fields = (
        event_time,
        available_time,
        watermark,
        sealed_at,
        max_clock_drift_seconds,
    )
    pit_fields = (event_asof, evidence_time, decision_mode, stage_policy)
    point_in_time = None
    if any(value is not None for value in pit_fields + strict_fields):
        if not all(value is not None for value in pit_fields):
            raise PointInTimeViolation("point_in_time_contract_incomplete")
        point_in_time = validate_point_in_time(
            event_asof=str(event_asof),
            evidence_time=str(evidence_time),
            captured_at=captured,
            decision_mode=str(decision_mode),
            stage_policy=stage_policy or {},
            event_time=event_time,
            available_time=available_time,
            watermark=watermark,
            sealed_at=sealed_at,
            max_clock_drift_seconds=max_clock_drift_seconds,
        )
    versions = dict(source_versions or infer_source_versions(payload))
    payload_hash = _hash(payload)
    seal_hash = None
    if point_in_time is not None and any(
        value is not None for value in strict_fields
    ):
        seal_hash = _seal_hash(payload_hash, point_in_time)
        point_in_time = {**point_in_time, "seal_hash": seal_hash}
    identity = {
        "dataset": dataset,
        "trading_date": trading_date,
        "batch_id": batch_id,
        "producer": producer,
        "producer_version": producer_version or "unknown",
        "payload_hash": payload_hash,
        "source_versions": versions,
        "point_in_time": point_in_time,
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
        "captured_at": captured,
        "point_in_time": point_in_time,
        "payload_schema": payload.get("schema") if isinstance(payload, Mapping) else None,
        "payload_hash": payload_hash,
        "source_versions": versions,
        "payload": payload,
        "snapshot_path": path,
    }
    if seal_hash is not None:
        record["seal_hash"] = seal_hash
    existing = read_json(path, None) if os.path.exists(path) else None
    if isinstance(existing, dict):
        if existing.get("payload_hash") != payload_hash:
            raise ValueError(f"immutable snapshot collision: {snapshot_id}")
        return read_snapshot(path)
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
    point_in_time = record.get("point_in_time")
    strict_keys = {
        "event_time",
        "available_time",
        "watermark",
        "sealed_at",
        "max_clock_drift_seconds",
        "seal_hash",
    }
    if isinstance(point_in_time, Mapping) and any(
        key in point_in_time for key in strict_keys
    ):
        if not strict_keys.issubset(point_in_time):
            raise ValueError(f"market snapshot strict PIT contract incomplete: {path}")
        seal_hash = point_in_time.get("seal_hash")
        if record.get("seal_hash") != seal_hash or seal_hash != _seal_hash(
            str(record.get("payload_hash")),
            point_in_time,
        ):
            raise ValueError(f"market snapshot seal hash mismatch: {path}")
    elif record.get("seal_hash") is not None:
        raise ValueError(f"market snapshot strict PIT contract incomplete: {path}")
    if record.get("snapshot_id") != _snapshot_id_from_record(record):
        raise ValueError(f"market snapshot identity mismatch: {path}")
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
    event_asof: Optional[str] = None,
    evidence_time: Optional[str] = None,
    decision_mode: Optional[str] = None,
    stage_policy: Optional[Mapping[str, Any]] = None,
    event_time: Optional[str] = None,
    available_time: Optional[str] = None,
    watermark: Optional[Mapping[str, Any]] = None,
    sealed_at: Optional[str] = None,
    max_clock_drift_seconds: Optional[float] = None,
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
        event_asof=event_asof,
        evidence_time=evidence_time,
        decision_mode=decision_mode,
        stage_policy=stage_policy,
        event_time=event_time,
        available_time=available_time,
        watermark=watermark,
        sealed_at=sealed_at,
        max_clock_drift_seconds=max_clock_drift_seconds,
    )
    loaded = read_snapshot(written)
    loaded["consumed_from_snapshot"] = True
    return loaded


def compact_ref(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the lineage fields safe to embed in downstream state."""
    reference = {
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
            "point_in_time",
            "consumed_from_snapshot",
        )
    }
    if snapshot.get("seal_hash") is not None:
        reference["seal_hash"] = snapshot.get("seal_hash")
    return reference
