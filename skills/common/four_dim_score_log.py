"""Append-only point-in-time observations for four-dimension weight research.

Only v2 rows are eligible for shadow fitting.  Legacy v1 rows remain readable
for the old calibration report, but are deliberately not upgraded by inference:
they do not contain enough provenance to prove point-in-time integrity.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from config_registry import config_path
from paths import data_file
from state_store import file_lock

SCHEMA = "four_dim_observation_v2"
LEGACY_SCHEMA = "four_dim_score_log_v1"
DIMENSIONS = ("technical", "sentiment", "catalyst", "deep")
_ROOT = Path(__file__).resolve().parents[2]
_SCORER_PATH = _ROOT / "skills" / "stock-triage" / "scripts" / "four_dim_scorer.py"


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text[2:] if text.startswith(("sh", "sz")) else text.zfill(6)


def log_path() -> str:
    return data_file("stock-triage", "four_dim_observations_v2.jsonl")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: str | os.PathLike[str]) -> str | None:
    try:
        return _sha256_bytes(Path(path).read_bytes())
    except OSError:
        return None


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return _sha256_bytes(payload.encode("utf-8"))


def recompute_input_bundle_sha256(observation: Mapping[str, Any]) -> str:
    """Recompute the content hash that binds an observation to its scored inputs."""
    snapshot = observation.get("input_snapshot") or {}
    return _stable_hash({
        "snapshot_sha256": snapshot.get("sha256"),
        "input_fingerprint_sha256": observation.get("input_fingerprint_sha256"),
        "dimensions": observation.get("dimensions") or {},
        "current_weights": observation.get("current_weights") or {},
        "effective_weights": observation.get("effective_weights") or {},
    })


def recompute_observation_id(observation: Mapping[str, Any]) -> str:
    """Recompute the append-only identity; consumers use this as an integrity gate."""
    snapshot = observation.get("input_snapshot") or {}
    versions = observation.get("versions") or {}
    return _stable_hash({
        "code": _code(observation.get("code")),
        "trading_date": observation.get("trading_date"),
        "strategy_lane": observation.get("strategy_lane"),
        "input_snapshot_sha256": snapshot.get("sha256"),
        "scorer_sha256": versions.get("scorer_sha256"),
        "config_sha256": versions.get("config_sha256"),
        "input_bundle_sha256": observation.get("input_bundle_sha256"),
        "dimensions": observation.get("dimensions") or {},
        "effective_weights": observation.get("effective_weights") or {},
    })


def _is_digest(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text.lower())


def _sub_score(scores: Mapping[str, Any], dim: str) -> float | None:
    block = scores.get(dim) if isinstance(scores, Mapping) else None
    value = block.get("score") if isinstance(block, Mapping) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return float(value)
    return None


def _numeric_weights(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, float] = {}
    for dim in DIMENSIONS:
        raw = value.get(dim)
        if isinstance(raw, str) and raw.endswith("%"):
            try:
                raw = float(raw[:-1]) / 100.0
            except ValueError:
                continue
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(float(raw)):
            result[dim] = float(raw)
    total = sum(result.values())
    return {key: value / total for key, value in result.items()} if total > 0 else {}


def _dimension_rows(item: Mapping[str, Any], asof: str) -> dict[str, dict[str, Any]]:
    scores = item.get("scores") if isinstance(item.get("scores"), Mapping) else {}
    provenance = item.get("dimension_provenance") if isinstance(item.get("dimension_provenance"), Mapping) else {}
    excluded = set(item.get("excluded_dims") or [])
    degraded = set(item.get("degraded_dims") or [])
    rows: dict[str, dict[str, Any]] = {}
    for dim in DIMENSIONS:
        meta = provenance.get(dim) if isinstance(provenance.get(dim), Mapping) else {}
        score = _sub_score(scores, dim)
        status = str(meta.get("status") or ("excluded" if dim in excluded else "degraded" if dim in degraded else "available"))
        source_asof = meta.get("asof")
        rows[dim] = {
            "score": score,
            "status": status,
            "source": str(meta.get("source") or "unknown"),
            "asof": str(source_asof) if source_asof else None,
        }
    return rows


def _point_in_time(
    dimensions: Mapping[str, Mapping[str, Any]], asof: str, *,
    snapshot_ref: str | None, snapshot_sha: str | None,
    scorer_sha: str | None, config_sha: str | None, input_sha: str | None,
    current_weights: Mapping[str, float], effective_weights: Mapping[str, float],
) -> dict[str, Any]:
    missing = []
    if not snapshot_ref or Path(snapshot_ref).name == "candidate_pool_latest.json":
        missing.append("input_snapshot_immutable_ref")
    if not _is_digest(snapshot_sha):
        missing.append("input_snapshot_sha256")
    for name, digest in (
        ("scorer_sha256", scorer_sha),
        ("config_sha256", config_sha),
        ("input_fingerprint_sha256", input_sha),
    ):
        if not _is_digest(digest):
            missing.append(name)
    for name, weights in (
        ("current_weights", current_weights),
        ("effective_weights", effective_weights),
    ):
        if set(weights) != set(DIMENSIONS) or not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-6):
            missing.append(name)
    try:
        cutoff = date.fromisoformat(asof)
    except ValueError:
        cutoff = None
        missing.append("trading_date")
    for dim, row in dimensions.items():
        if row.get("status") != "available":
            missing.append(f"{dim}.status")
        if row.get("score") is None:
            missing.append(f"{dim}.score")
        if str(row.get("source") or "") in {"", "unknown", "unavailable"}:
            missing.append(f"{dim}.source")
        source_asof = row.get("asof")
        if not source_asof:
            missing.append(f"{dim}.asof")
            continue
        try:
            source_day = date.fromisoformat(str(source_asof)[:10])
        except ValueError:
            missing.append(f"{dim}.asof_invalid")
            continue
        if cutoff is not None and source_day > cutoff:
            missing.append(f"{dim}.future_dated")
    return {"status": "complete" if not missing else "incomplete", "missing": sorted(set(missing))}


def _observation(
    item: Mapping[str, Any], *, asof: str, recorded_at: str,
    snapshot_ref: str | None, snapshot_sha: str | None,
    scorer_sha: str | None, config_sha: str | None,
) -> dict[str, Any] | None:
    dimensions = _dimension_rows(item, asof)
    if all(row["score"] is None for row in dimensions.values()):
        return None
    current = _numeric_weights(item.get("weight_values") or item.get("weights"))
    effective = _numeric_weights(item.get("effective_weight_values") or item.get("effective_weights"))
    input_sha = str(item.get("input_fingerprint_sha256") or "") or None
    observation = {
        "schema": SCHEMA,
        "code": _code(item.get("code")),
        "trading_date": asof,
        "observed_at": recorded_at,
        "strategy_lane": item.get("strategy_lane"),
        "dimensions": dimensions,
        "weighted_score": item.get("weighted"),
        "grade": item.get("grade"),
        "current_weights": current,
        "effective_weights": effective,
        "input_snapshot": {"ref": snapshot_ref, "sha256": snapshot_sha},
        "input_fingerprint_sha256": input_sha,
        "versions": {
            "scorer_sha256": scorer_sha,
            "config_sha256": config_sha,
            "contract_sha256": _stable_hash({"schema": SCHEMA, "dimensions": DIMENSIONS}),
        },
        "point_in_time": _point_in_time(
            dimensions, asof,
            snapshot_ref=snapshot_ref, snapshot_sha=snapshot_sha,
            scorer_sha=scorer_sha, config_sha=config_sha, input_sha=input_sha,
            current_weights=current, effective_weights=effective,
        ),
        "research_only": True,
        "live_effect": "none",
    }
    observation["input_bundle_sha256"] = recompute_input_bundle_sha256(observation)
    observation["observation_id"] = recompute_observation_id(observation)
    return observation


def record_scores(
    batch_result: Mapping[str, Any], *, asof: str, path: str | None = None,
    input_snapshot_path: str | None = None, recorded_at: str | None = None,
) -> int:
    """Append deduplicated v2 observations without affecting scorer success."""
    results = batch_result.get("results") if isinstance(batch_result, Mapping) else None
    if not isinstance(results, list):
        return 0
    now = recorded_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshot_sha = _file_sha256(input_snapshot_path) if input_snapshot_path else None
    scorer_sha = _file_sha256(_SCORER_PATH)
    config_sha = _file_sha256(config_path("scoring"))
    rows = [
        row for item in results
        if isinstance(item, Mapping) and item.get("status") != "failed"
        if (row := _observation(
            item, asof=asof, recorded_at=now,
            snapshot_ref=input_snapshot_path, snapshot_sha=snapshot_sha,
            scorer_sha=scorer_sha, config_sha=config_sha,
        )) is not None
    ]
    if not rows:
        return 0
    target = path or log_path()
    try:
        with file_lock(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            existing = {row.get("observation_id") for row in load_scores(target)} if os.path.exists(target) else set()
            fresh = [row for row in rows if row["observation_id"] not in existing]
            if not fresh:
                return 0
            with open(target, "a", encoding="utf-8") as handle:
                for row in fresh:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            return len(fresh)
    except (OSError, TimeoutError):
        return 0


def load_scores(path: str | None = None, *, schemas: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Read valid JSON objects; callers may restrict eligible schema versions."""
    target = path or log_path()
    if not os.path.exists(target):
        return []
    allowed = set(schemas) if schemas is not None else None
    rows: list[dict[str, Any]] = []
    with open(target, encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and (allowed is None or value.get("schema") in allowed):
                rows.append(value)
    return rows
