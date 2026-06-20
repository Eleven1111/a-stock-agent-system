"""Tamper-evident research artifacts bound to their source input file."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Any, Mapping

from state_store import atomic_write_json


SCHEMA = "strategy_research_artifact_v1"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_digest(artifact: Mapping[str, Any]) -> str:
    body = {key: value for key, value in artifact.items() if key != "artifact_sha256"}
    return json_sha256(body)


def build_artifact(
    *,
    input_path: str,
    strategy_id: str,
    rules: Mapping[str, Any],
    result: Mapping[str, Any],
    gate_metrics: Mapping[str, Any],
    control_counts: Mapping[str, int],
) -> dict[str, Any]:
    source_path = os.path.abspath(os.path.expanduser(input_path))
    if not os.path.isfile(source_path):
        raise FileNotFoundError(source_path)
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "strategy_id": str(strategy_id),
        "source": {
            "input_path": source_path,
            "input_sha256": file_sha256(source_path),
        },
        "rules": dict(rules),
        "rules_sha256": json_sha256(dict(rules)),
        "result": dict(result),
        "result_sha256": json_sha256(dict(result)),
        "gate_metrics": dict(gate_metrics),
        "control_counts": {
            str(name): int(count) for name, count in control_counts.items()
        },
    }
    artifact["artifact_sha256"] = _artifact_digest(artifact)
    return artifact


def write_artifact(path: str, **kwargs: Any) -> dict[str, Any]:
    artifact = build_artifact(**kwargs)
    atomic_write_json(os.path.abspath(os.path.expanduser(path)), artifact)
    return artifact


def verify_artifact(path: str, *, expected_sha256: str | None = None) -> dict[str, Any]:
    artifact_path = os.path.abspath(os.path.expanduser(path))
    errors: list[str] = []
    try:
        with open(artifact_path, encoding="utf-8") as handle:
            artifact = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"artifact_unreadable:{exc}"], "artifact": None}
    if not isinstance(artifact, dict) or artifact.get("schema") != SCHEMA:
        return {"valid": False, "errors": ["unsupported_artifact_schema"], "artifact": artifact}

    actual_artifact_digest = _artifact_digest(artifact)
    if artifact.get("artifact_sha256") != actual_artifact_digest:
        errors.append("artifact_sha256_mismatch")
    if expected_sha256 and actual_artifact_digest != expected_sha256:
        errors.append("expected_artifact_sha256_mismatch")
    if artifact.get("rules_sha256") != json_sha256(artifact.get("rules") or {}):
        errors.append("rules_sha256_mismatch")
    if artifact.get("result_sha256") != json_sha256(artifact.get("result") or {}):
        errors.append("result_sha256_mismatch")

    source = artifact.get("source") or {}
    input_path = source.get("input_path")
    if not input_path or not os.path.isfile(input_path):
        errors.append("source_input_missing")
    else:
        try:
            if source.get("input_sha256") != file_sha256(input_path):
                errors.append("source_input_sha256_mismatch")
        except OSError:
            errors.append("source_input_unreadable")

    metrics = artifact.get("gate_metrics")
    if not isinstance(metrics, dict):
        errors.append("gate_metrics_missing")
    controls = artifact.get("control_counts")
    if not isinstance(controls, dict):
        errors.append("control_counts_missing")

    return {"valid": not errors, "errors": errors, "artifact": artifact}
