"""Immutable point-in-time fundamental facts for the research plane.

Provider adapters supply both the financial facts and their observed timeline.
The accounting ``asof`` date is never used as evidence, capture, publication,
or availability time.  Missing numeric values remain null.
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime
from typing import Any, Mapping

from market_snapshot import (
    PointInTimeViolation,
    build_stage_policy,
    read_snapshot,
    write_snapshot,
)
from paths import data_file
from state_store import mutate_json, read_json


SCHEMA = "fundamental_facts_v1"
DATASET = "fundamental_facts"
INDEX_SCHEMA = "fundamental_facts_index_v2"
TIMEZONE_NAME = "Asia/Shanghai"
MAX_CLOCK_DRIFT_SECONDS = 86_400
DEFAULT_MAX_AGE_DAYS = 7.0


def index_file() -> str:
    return data_file("research-committee", "fundamentals_latest.json")


def _code(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    text = str(value or "").strip()
    return text.zfill(6) if text.isdigit() else text


def _aware_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PointInTimeViolation(f"{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PointInTimeViolation(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PointInTimeViolation(f"{field}_timezone_missing")
    return parsed


def _trading_day(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("trading_date_invalid")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("trading_date_invalid") from exc
    return value


def _numeric_or_none(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field}_non_numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_non_numeric") from exc
    return number if math.isfinite(number) else None


def _strict_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}_invalid")
    return value.strip()


def _string_mapping(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field}_invalid")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        name = _strict_string(key, f"{field}_key")
        normalized[name] = _strict_string(item, f"{field}_{name}")
    return normalized


def _numeric_mapping(value: Any, field: str) -> dict[str, float | None]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field}_missing")
    normalized: dict[str, float | None] = {}
    for key, item in value.items():
        name = _strict_string(key, f"{field}_key")
        normalized[name] = _numeric_or_none(item, f"{field}_{name}")
    return normalized


def _quality_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("quality_invalid")
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        name = _strict_string(key, "quality_key")
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError(f"quality_{name}_invalid")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"quality_{name}_invalid")
        normalized[name] = item
    return normalized


def _normalize_payload(code: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("fundamental facts must be an object")
    normalized_code = _code(code)
    if not normalized_code or not normalized_code.isdigit() or len(normalized_code) != 6:
        raise ValueError("code_invalid")
    payload_code = payload.get("code")
    if payload_code not in (None, "") and _code(payload_code) != normalized_code:
        raise ValueError("code_mismatch")

    raw_asof = _strict_string(payload.get("asof"), "asof")
    if len(raw_asof) != 10:
        raise ValueError("asof_invalid")
    try:
        date.fromisoformat(raw_asof)
    except ValueError as exc:
        raise ValueError("asof_invalid") from exc

    source = _string_mapping(payload.get("source"), "source")
    if "provider" not in source:
        raise ValueError("source_provider_missing")
    units = _string_mapping(payload.get("units"), "units")
    if "scale" not in units:
        raise ValueError("units_scale_missing")
    restated = payload.get("restated", False)
    if not isinstance(restated, bool):
        raise ValueError("restated_invalid")

    raw_periods = payload.get("periods")
    if not isinstance(raw_periods, list) or not raw_periods:
        raise ValueError("periods_missing")
    periods: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_periods[:8]):
        if not isinstance(raw, Mapping):
            raise ValueError(f"period_{index}_invalid")
        period = _strict_string(raw.get("period"), f"period_{index}_period")
        item: dict[str, Any] = {"period": period}
        for key, value in raw.items():
            if key == "period":
                continue
            name = _strict_string(key, f"period_{index}_key")
            item[name] = _numeric_or_none(
                value,
                f"period_{index}_{name}",
            )
        periods.append(item)

    return {
        "schema": SCHEMA,
        "code": normalized_code,
        "name": _strict_string(payload.get("name"), "name"),
        "asof": raw_asof,
        "source": source,
        "units": units,
        "restated": restated,
        "metrics": _numeric_mapping(payload.get("metrics"), "metrics"),
        "valuation": _numeric_mapping(payload.get("valuation", {}), "valuation"),
        "periods": periods,
        "quality": _quality_mapping(payload.get("quality", {})),
    }


def validate_fundamental_facts(
    facts: Mapping[str, Any], *, code: str | None = None,
) -> list[str]:
    """Validate an already normalized fundamental-facts payload."""
    errors: list[str] = []
    if facts.get("schema") != SCHEMA:
        errors.append("schema_invalid")
    normalized_code = _code(code or facts.get("code"))
    if not normalized_code or not normalized_code.isdigit() or len(normalized_code) != 6:
        errors.append("code_invalid")
    elif _code(facts.get("code")) != normalized_code:
        errors.append("code_mismatch")
    try:
        asof = facts.get("asof")
        if not isinstance(asof, str) or len(asof) != 10:
            raise ValueError
        date.fromisoformat(asof)
    except (TypeError, ValueError):
        errors.append("asof_invalid")
    source = facts.get("source")
    if (
        not isinstance(source, Mapping)
        or not isinstance(source.get("provider"), str)
        or not source.get("provider", "").strip()
    ):
        errors.append("source_provider_missing")
    units = facts.get("units")
    if (
        not isinstance(units, Mapping)
        or not isinstance(units.get("scale"), str)
        or not units.get("scale", "").strip()
    ):
        errors.append("units_scale_missing")
    if not isinstance(facts.get("restated"), bool):
        errors.append("restated_invalid")
    if not isinstance(facts.get("metrics"), Mapping):
        errors.append("metrics_missing")
    if not isinstance(facts.get("valuation"), Mapping):
        errors.append("valuation_missing")
    if not isinstance(facts.get("periods"), list) or not facts.get("periods"):
        errors.append("periods_missing")
    if not isinstance(facts.get("quality"), Mapping):
        errors.append("quality_invalid")
    return list(dict.fromkeys(errors))


def _validate_observed_timeline(
    *,
    facts: Mapping[str, Any],
    event_time: str,
    published_at: str,
    available_at: str,
    captured_at: str,
    watermark: Mapping[str, Any],
    sealed_at: str,
) -> None:
    event = _aware_datetime(event_time, "event_time")
    published = _aware_datetime(published_at, "published_at")
    available = _aware_datetime(available_at, "available_at")
    captured = _aware_datetime(captured_at, "captured_at")
    sealed = _aware_datetime(sealed_at, "sealed_at")
    if not event <= published <= available <= captured <= sealed:
        raise PointInTimeViolation("fundamental_time_order_invalid")
    if date.fromisoformat(str(facts["asof"])) > event.date():
        raise PointInTimeViolation("asof_after_event")
    if not isinstance(watermark, Mapping):
        raise PointInTimeViolation("watermark_invalid")
    watermark_published = _aware_datetime(
        watermark.get("provider_published_at"),
        "watermark_provider_published_at",
    )
    if watermark_published != published:
        raise PointInTimeViolation("watermark_publication_mismatch")


def _index_versions(value: Any, code: str) -> list[dict[str, Any]]:
    if value in (None, {}):
        return []
    if not isinstance(value, Mapping) or value.get("schema") != INDEX_SCHEMA:
        raise ValueError("fundamental_index_invalid")
    entries = value.get("entries")
    if not isinstance(entries, Mapping):
        raise ValueError("fundamental_index_invalid")
    versions = entries.get(code, [])
    if not isinstance(versions, list) or not all(
        isinstance(item, dict) for item in versions
    ):
        raise ValueError("fundamental_index_invalid")
    return [dict(item) for item in versions]


def _version_entry(snapshot: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
    point_in_time = snapshot.get("point_in_time")
    if not isinstance(point_in_time, Mapping):
        raise ValueError("fundamental_snapshot_pit_missing")
    return {
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_path": snapshot.get("snapshot_path"),
        "trading_date": snapshot.get("trading_date"),
        "asof": facts.get("asof"),
        "restated": facts.get("restated"),
        "event_time": point_in_time.get("event_time"),
        "published_at": point_in_time.get("evidence_time"),
        "available_at": point_in_time.get("available_time"),
        "captured_at": point_in_time.get("captured_at"),
        "sealed_at": point_in_time.get("sealed_at"),
        "payload_hash": snapshot.get("payload_hash"),
        "seal_hash": snapshot.get("seal_hash"),
    }


def _duplicate_version(
    versions: list[dict[str, Any]],
    *,
    event_time: str,
    published_at: str,
    available_at: str,
) -> bool:
    identity = (
        _aware_datetime(event_time, "event_time"),
        _aware_datetime(published_at, "published_at"),
        _aware_datetime(available_at, "available_at"),
    )
    return any(
        (
            _aware_datetime(item.get("event_time"), "event_time"),
            _aware_datetime(item.get("published_at"), "published_at"),
            _aware_datetime(item.get("available_at"), "available_at"),
        )
        == identity
        for item in versions
    )


def _validate_restatement_availability(
    versions: list[dict[str, Any]],
    *,
    facts: Mapping[str, Any],
    available_at: str,
) -> None:
    if facts.get("restated") is not True:
        return
    same_period = [
        _aware_datetime(item.get("available_at"), "available_at")
        for item in versions
        if item.get("asof") == facts.get("asof")
    ]
    if same_period and _aware_datetime(available_at, "available_at") <= max(same_period):
        raise ValueError("restatement_availability_not_newer")


def write_fundamental_snapshot(
    code: str,
    payload: Mapping[str, Any],
    *,
    trading_date: str,
    batch_id: str,
    producer: str,
    producer_version: str,
    event_time: str,
    published_at: str,
    available_at: str,
    captured_at: str,
    watermark: Mapping[str, Any],
    sealed_at: str,
    source_versions: Mapping[str, str] | None = None,
    max_clock_drift_seconds: float = MAX_CLOCK_DRIFT_SECONDS,
) -> dict[str, Any]:
    """Write one provider-observed, strictly sealed fundamental version."""
    trading_day = _trading_day(trading_date)
    normalized_batch_id = _strict_string(batch_id, "batch_id")
    normalized_producer = _strict_string(producer, "producer")
    normalized_producer_version = _strict_string(
        producer_version,
        "producer_version",
    )
    normalized_source_versions = (
        _string_mapping(source_versions, "source_versions")
        if source_versions is not None
        else None
    )
    facts = _normalize_payload(code, payload)
    errors = validate_fundamental_facts(facts, code=code)
    if errors:
        raise ValueError("invalid fundamental facts: " + ",".join(errors))
    _validate_observed_timeline(
        facts=facts,
        event_time=event_time,
        published_at=published_at,
        available_at=available_at,
        captured_at=captured_at,
        watermark=watermark,
        sealed_at=sealed_at,
    )
    normalized_code = _code(code)
    current_index = read_json(index_file(), {})
    current_versions = _index_versions(current_index, normalized_code)
    if _duplicate_version(
        current_versions,
        event_time=event_time,
        published_at=published_at,
        available_at=available_at,
    ):
        raise ValueError("duplicate_fundamental_version")
    _validate_restatement_availability(
        current_versions,
        facts=facts,
        available_at=available_at,
    )

    snapshot = write_snapshot(
        DATASET,
        facts,
        trading_date=trading_day,
        batch_id=normalized_batch_id,
        producer=normalized_producer,
        producer_version=normalized_producer_version,
        source_versions=normalized_source_versions,
        captured_at=captured_at,
        event_asof=trading_day,
        evidence_time=published_at,
        decision_mode="replay",
        stage_policy=build_stage_policy(
            stage="fundamental_facts",
            cutoff_time="23:59:59",
            timezone_name=TIMEZONE_NAME,
        ),
        event_time=event_time,
        available_time=available_at,
        watermark=watermark,
        sealed_at=sealed_at,
        max_clock_drift_seconds=max_clock_drift_seconds,
    )
    new_entry = _version_entry(snapshot, facts)

    def _mutate(value: Any) -> dict[str, Any]:
        versions = _index_versions(value, normalized_code)
        if _duplicate_version(
            versions,
            event_time=event_time,
            published_at=published_at,
            available_at=available_at,
        ):
            raise ValueError("duplicate_fundamental_version")
        _validate_restatement_availability(
            versions,
            facts=facts,
            available_at=available_at,
        )
        versions.append(new_entry)
        versions.sort(
            key=lambda item: (
                str(item.get("available_at") or ""),
                str(item.get("sealed_at") or ""),
                str(item.get("snapshot_id") or ""),
            )
        )
        entries = dict(value.get("entries") or {}) if isinstance(value, Mapping) else {}
        entries[normalized_code] = versions
        return {"schema": INDEX_SCHEMA, "entries": entries}

    mutate_json(index_file(), _mutate, {})
    return snapshot


def _decision_cutoff(
    *, trading_date: str | None, decision_cutoff: str | None,
) -> datetime:
    if decision_cutoff is None:
        if trading_date is None:
            raise ValueError("decision_cutoff_required")
        try:
            date.fromisoformat(str(trading_date))
        except (TypeError, ValueError) as exc:
            raise ValueError("trading_date_invalid") from exc
        decision_cutoff = f"{trading_date}T15:00:00+08:00"
    cutoff = _aware_datetime(decision_cutoff, "decision_cutoff")
    if trading_date is not None and cutoff.date().isoformat() != str(trading_date):
        raise ValueError("decision_cutoff_trading_date_mismatch")
    return cutoff


def _max_age_days(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("max_age_days_invalid")
    try:
        days = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_age_days_invalid") from exc
    if not math.isfinite(days) or days < 0:
        raise ValueError("max_age_days_invalid")
    return days


def read_latest_fundamentals(
    code: str,
    *,
    trading_date: str | None = None,
    decision_cutoff: str | None = None,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
) -> dict[str, Any] | None:
    """Read the newest available version and evaluate its actual evidence age."""
    cutoff = _decision_cutoff(
        trading_date=trading_date,
        decision_cutoff=decision_cutoff,
    )
    freshness_window = _max_age_days(max_age_days)
    versions = _index_versions(read_json(index_file(), {}), _code(code))
    eligible: list[tuple[datetime, dict[str, Any]]] = []
    for entry in versions:
        available = _aware_datetime(entry.get("available_at"), "available_at")
        if available <= cutoff:
            eligible.append((available, entry))
    if not eligible:
        return None
    _, entry = max(
        eligible,
        key=lambda item: (
            item[0],
            str(item[1].get("sealed_at") or ""),
            str(item[1].get("snapshot_id") or ""),
        ),
    )
    path = entry.get("snapshot_path")
    if not isinstance(path, str) or not path or not os.path.exists(path):
        return None
    try:
        snapshot = read_snapshot(path)
    except (OSError, ValueError):
        return None
    payload = snapshot.get("payload")
    point_in_time = snapshot.get("point_in_time")
    if (
        not isinstance(payload, dict)
        or _code(payload.get("code")) != _code(code)
        or not isinstance(point_in_time, Mapping)
        or point_in_time.get("available_time") != entry.get("available_at")
    ):
        return None
    available = _aware_datetime(point_in_time.get("available_time"), "available_at")
    captured = _aware_datetime(point_in_time.get("captured_at"), "captured_at")
    available_age_days = (cutoff - available).total_seconds() / 86_400
    captured_age_days = (cutoff - captured).total_seconds() / 86_400
    age_days = max(available_age_days, captured_age_days)
    snapshot_day = str(snapshot.get("trading_date") or "")[:10]
    result = dict(payload)
    result.update(
        {
            "snapshot_ref": snapshot.get("snapshot_id"),
            "snapshot_path": snapshot.get("snapshot_path"),
            "snapshot_trading_date": snapshot_day,
            "event_time": point_in_time.get("event_time"),
            "published_at": point_in_time.get("evidence_time"),
            "available_at": point_in_time.get("available_time"),
            "captured_at": point_in_time.get("captured_at"),
            "watermark": point_in_time.get("watermark"),
            "sealed_at": point_in_time.get("sealed_at"),
            "available_age_days": available_age_days,
            "captured_age_days": captured_age_days,
            "age_days": age_days,
            "max_age_days": freshness_window,
        }
    )
    result["stale"] = age_days > freshness_window
    result["evidence_status"] = "stale" if result["stale"] else "fresh"
    return result
