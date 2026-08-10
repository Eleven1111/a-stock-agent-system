"""Isolated execution and independent validation for sealed analysis plans."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import analysis_plan
import dataset_contract
import dual_agent_compiler
from paths import data_file
from state_store import atomic_write_json


EXECUTION_SCHEMA = "deterministic_execution_v1"
VALIDATION_SCHEMA = "deterministic_validation_v1"
EXECUTOR_VERSION = "isolated-analysis-executor-v1"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_STDOUT_CHARS = 2_000_000
REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "run_analysis_plan.py"


class DeterministicExecutorError(ValueError):
    """An execution request or persisted validation artifact is invalid."""

    def __init__(self, *errors: str) -> None:
        self.errors = tuple(dict.fromkeys(str(error) for error in errors if error))
        super().__init__("; ".join(self.errors) or "deterministic_executor_invalid")


def default_store_dir() -> str:
    return data_file("research-committee", "validated_executions")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DeterministicExecutorError("payload_not_canonical_json") from exc


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _aware(value: str | None) -> str:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DeterministicExecutorError("validated_at_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeterministicExecutorError("validated_at_timezone_required")
    return parsed.isoformat()


def _artifact(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "execution_hash": _hash(body)}


def _validation(
    *,
    status: str,
    validated_at: str,
    compilation_hash: str | None,
    plan_hash: str | None,
    catalog_hash: str | None,
    input_hash: str | None,
    result_hash: str | None,
    replay_count: int,
    replay_deterministic: bool,
    checks: list[dict[str, str]],
    reason_codes: list[str],
) -> dict[str, Any]:
    body = {
        "schema": VALIDATION_SCHEMA,
        "executor_version": EXECUTOR_VERSION,
        "status": status,
        "validated_at": validated_at,
        "compilation_hash": compilation_hash,
        "plan_hash": plan_hash,
        "catalog_hash": catalog_hash,
        "input_hash": input_hash,
        "result_hash": result_hash,
        "replay_count": replay_count,
        "replay_deterministic": replay_deterministic,
        "checks": checks,
        "reason_codes": list(dict.fromkeys(reason_codes)),
    }
    return {**body, "validation_hash": _hash(body)}


def _blocked(
    *,
    validated_at: str,
    compilation: Mapping[str, Any],
    input_hash: str | None,
    reasons: list[str],
    checks: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    reason_codes = list(dict.fromkeys(reasons or ["execution_blocked"]))
    validation = _validation(
        status="failed",
        validated_at=validated_at,
        compilation_hash=compilation.get("compilation_hash"),
        plan_hash=(compilation.get("sealed_plan") or {}).get("plan_hash")
        if isinstance(compilation.get("sealed_plan"), Mapping)
        else None,
        catalog_hash=compilation.get("catalog_hash"),
        input_hash=input_hash,
        result_hash=None,
        replay_count=0,
        replay_deterministic=False,
        checks=checks or [],
        reason_codes=reason_codes,
    )
    body = {
        "schema": EXECUTION_SCHEMA,
        "executor_version": EXECUTOR_VERSION,
        "status": "blocked",
        "validated_at": validated_at,
        "compilation_hash": compilation.get("compilation_hash"),
        "input_hash": input_hash,
        "reason_codes": reason_codes,
        "validation": validation,
        "research_only": True,
        "trading_action": "none",
    }
    return _artifact(body)


def _workspace_root(value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise DeterministicExecutorError("workspace_root_invalid")
    return path.resolve()


def _child_environment(state_home: Path) -> dict[str, str]:
    return {
        "A_STOCK_STATE_HOME": str(state_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": str(REPO_ROOT),
        "TZ": "Asia/Shanghai",
    }


def _child_errors(stdout: str) -> list[str]:
    try:
        value = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        return ["executor_failed"]
    errors = value.get("errors") if isinstance(value, Mapping) else None
    if not isinstance(errors, list) or not errors:
        return ["executor_failed"]
    return [str(error)[:500] for error in errors]


def _normalized_run(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in (
            "schema",
            "engine_version",
            "plan_id",
            "plan_hash",
            "catalog_hash",
            "input_hash",
            "cache_key",
            "research_only",
            "trading_action",
            "outputs",
            "lineage",
            "result_hash",
        )
        if key in value
    }


def _run_once(
    plan: Mapping[str, Any],
    inputs: Mapping[str, Any],
    catalog: Mapping[str, Any],
    *,
    workspace_root: Path | None,
    timeout_seconds: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    with tempfile.TemporaryDirectory(
        prefix="a-stock-deterministic-exec-",
        dir=str(workspace_root) if workspace_root else None,
    ) as directory:
        workspace = Path(directory)
        plan_path = workspace / "plan.json"
        input_path = workspace / "inputs.json"
        catalog_path = workspace / "catalog.json"
        cache_dir = workspace / "cache"
        state_home = workspace / "state"
        atomic_write_json(str(plan_path), dict(plan))
        atomic_write_json(str(input_path), dict(inputs))
        atomic_write_json(str(catalog_path), dict(catalog))
        argv = [
            sys.executable,
            str(RUNNER),
            "--plan",
            str(plan_path),
            "--inputs",
            str(input_path),
            "--catalog",
            str(catalog_path),
            "--cache-dir",
            str(cache_dir),
        ]
        try:
            completed = subprocess.run(
                argv,
                cwd=str(workspace),
                env=_child_environment(state_home),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return None, ["executor_timeout"]
        stdout = str(completed.stdout or "")
        if len(stdout) > MAX_STDOUT_CHARS:
            return None, ["executor_output_too_large"]
        if completed.returncode != 0:
            return None, _child_errors(stdout)
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            return None, ["executor_output_invalid"]
        if not isinstance(result, Mapping) or not analysis_plan.verify_run_result(result):
            return None, ["execution_result_hash_mismatch"]
        return _normalized_run(result), []


def _preflight(
    compilation: Mapping[str, Any], catalog: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    try:
        verified = dual_agent_compiler.verify_compilation(compilation)
    except dual_agent_compiler.DualAgentCompilerError as exc:
        return None, None, list(exc.errors)
    if (
        verified.get("status") != "compiled"
        or verified.get("handoff_status") != "ready_for_deterministic_execution"
    ):
        return verified, None, ["handoff_not_ready"]
    if verified.get("catalog_hash") != catalog.get("catalog_hash"):
        return verified, None, ["catalog_hash_mismatch"]
    try:
        sealed_catalog = dataset_contract.seal_catalog(catalog)
        sealed_plan = analysis_plan.seal_plan(
            verified.get("sealed_plan") or {}, catalog=sealed_catalog
        )
    except (dataset_contract.DatasetContractError, analysis_plan.AnalysisPlanError) as exc:
        return verified, None, list(getattr(exc, "errors", (str(exc),)))
    if sealed_plan != verified.get("sealed_plan"):
        return verified, None, ["sealed_plan_identity_mismatch"]
    return verified, sealed_catalog, []


def _replay_plan(
    verified: Mapping[str, Any],
    inputs: Mapping[str, Any],
    sealed_catalog: Mapping[str, Any],
    *,
    root: Path | None,
    timeout_seconds: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    plan = verified["sealed_plan"]
    first, reasons = _run_once(
        plan,
        inputs,
        sealed_catalog,
        workspace_root=root,
        timeout_seconds=timeout_seconds,
    )
    if reasons or first is None:
        return None, reasons
    second, reasons = _run_once(
        plan,
        inputs,
        sealed_catalog,
        workspace_root=root,
        timeout_seconds=timeout_seconds,
    )
    if reasons or second is None:
        return None, reasons
    if first != second:
        return None, ["replay_nondeterministic"]
    return first, []


def _validated_execution(
    verified: Mapping[str, Any],
    sealed_catalog: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    input_hash: str,
    timestamp: str,
) -> dict[str, Any]:
    plan = verified["sealed_plan"]
    checks = [
        {"name": "compilation_integrity", "status": "passed"},
        {"name": "catalog_and_plan_binding", "status": "passed"},
        {"name": "input_contracts", "status": "passed"},
        {"name": "isolated_subprocess", "status": "passed"},
        {"name": "result_hash", "status": "passed"},
        {"name": "deterministic_replay", "status": "passed"},
    ]
    validation = _validation(
        status="passed",
        validated_at=timestamp,
        compilation_hash=verified["compilation_hash"],
        plan_hash=plan["plan_hash"],
        catalog_hash=sealed_catalog["catalog_hash"],
        input_hash=input_hash,
        result_hash=run["result_hash"],
        replay_count=2,
        replay_deterministic=True,
        checks=checks,
        reason_codes=[],
    )
    body = {
        "schema": EXECUTION_SCHEMA,
        "executor_version": EXECUTOR_VERSION,
        "status": "validated",
        "validated_at": timestamp,
        "compilation_hash": verified["compilation_hash"],
        "input_hash": input_hash,
        "run": dict(run),
        "reason_codes": [],
        "validation": validation,
        "research_only": True,
        "trading_action": "none",
    }
    return _artifact(body)


def execute_compilation(
    compilation: Mapping[str, Any],
    inputs: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any],
    validated_at: str,
    workspace_root: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Execute the same sealed plan twice and validate deterministic replay."""

    timestamp = _aware(validated_at)
    compilation_value = dict(compilation)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 300
    ):
        raise DeterministicExecutorError("timeout_seconds_invalid")
    try:
        input_hash = _hash(inputs)
    except DeterministicExecutorError as exc:
        return _blocked(
            validated_at=timestamp,
            compilation=compilation_value,
            input_hash=None,
            reasons=list(exc.errors),
        )
    verified, sealed_catalog, reasons = _preflight(compilation_value, catalog)
    if reasons or verified is None or sealed_catalog is None:
        return _blocked(
            validated_at=timestamp,
            compilation=compilation_value,
            input_hash=input_hash,
            reasons=reasons,
        )
    run, reasons = _replay_plan(
        verified,
        inputs,
        sealed_catalog,
        root=_workspace_root(workspace_root),
        timeout_seconds=timeout_seconds,
    )
    if reasons or run is None:
        return _blocked(
            validated_at=timestamp,
            compilation=verified,
            input_hash=input_hash,
            reasons=reasons,
        )
    return _validated_execution(
        verified,
        sealed_catalog,
        run,
        input_hash=input_hash,
        timestamp=timestamp,
    )


def _verify_execution(
    value: Any, expected_hash: str | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != EXECUTION_SCHEMA:
        raise DeterministicExecutorError("execution_schema_invalid")
    body = {key: item for key, item in value.items() if key != "execution_hash"}
    actual = _hash(body)
    if value.get("execution_hash") != actual or (
        expected_hash is not None and actual != expected_hash
    ):
        raise DeterministicExecutorError("execution_hash_mismatch")
    if value.get("research_only") is not True or value.get("trading_action") != "none":
        raise DeterministicExecutorError("execution_boundary_invalid")
    if value.get("status") not in {"validated", "blocked"}:
        raise DeterministicExecutorError("execution_status_invalid")
    validation = value.get("validation")
    if not isinstance(validation, Mapping) or validation.get("schema") != VALIDATION_SCHEMA:
        raise DeterministicExecutorError("validation_schema_invalid")
    validation_body = {
        key: item for key, item in validation.items() if key != "validation_hash"
    }
    if validation.get("validation_hash") != _hash(validation_body):
        raise DeterministicExecutorError("validation_hash_mismatch")
    expected_status = "passed" if value.get("status") == "validated" else "failed"
    if validation.get("status") != expected_status:
        raise DeterministicExecutorError("validation_status_mismatch")
    if validation.get("compilation_hash") != value.get("compilation_hash"):
        raise DeterministicExecutorError("validation_compilation_hash_mismatch")
    if validation.get("input_hash") != value.get("input_hash"):
        raise DeterministicExecutorError("validation_input_hash_mismatch")
    if value.get("status") == "validated":
        run = value.get("run")
        if not isinstance(run, Mapping) or not analysis_plan.verify_run_result(run):
            raise DeterministicExecutorError("execution_run_hash_mismatch")
        if validation.get("result_hash") != run.get("result_hash"):
            raise DeterministicExecutorError("validation_result_hash_mismatch")
        for field in ("plan_hash", "catalog_hash", "input_hash"):
            if validation.get(field) != run.get(field):
                raise DeterministicExecutorError(f"validation_{field}_mismatch")
        if (
            validation.get("replay_count") != 2
            or validation.get("replay_deterministic") is not True
        ):
            raise DeterministicExecutorError("validation_replay_mismatch")
    elif "run" in value:
        raise DeterministicExecutorError("blocked_execution_run_forbidden")
    elif validation.get("reason_codes") != value.get("reason_codes"):
        raise DeterministicExecutorError("validation_reason_codes_mismatch")
    return value


def store_execution(
    value: Mapping[str, Any], *, store_dir: str | None = None
) -> dict[str, Any]:
    artifact = _verify_execution(dict(value))
    digest = artifact["execution_hash"].removeprefix("sha256:")
    path = Path(store_dir or default_store_dir()) / f"{digest}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeterministicExecutorError("execution_unreadable") from exc
        _verify_execution(existing, artifact["execution_hash"])
        return {"created": False, "execution": existing, "artifact_path": str(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(path), artifact)
    return {"created": True, "execution": artifact, "artifact_path": str(path)}


def load_execution(
    execution_hash: str, *, store_dir: str | None = None
) -> dict[str, Any]:
    normalized = str(execution_hash or "")
    if not normalized.startswith("sha256:") or len(normalized) != 71:
        raise DeterministicExecutorError("execution_hash_invalid")
    path = Path(store_dir or default_store_dir()) / (
        f"{normalized.removeprefix('sha256:')}.json"
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeterministicExecutorError("execution_unreadable") from exc
    return _verify_execution(value, normalized)


__all__ = [
    "DeterministicExecutorError",
    "EXECUTION_SCHEMA",
    "VALIDATION_SCHEMA",
    "default_store_dir",
    "execute_compilation",
    "load_execution",
    "store_execution",
]
