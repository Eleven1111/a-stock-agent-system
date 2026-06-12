"""Runtime context helpers shared by Hermes and OpenClaw.

Cron jobs must not rely on the active user conversation for intermediate data.
This module gives every job run a stable run id, an artifact path, upstream
context lookup, and a compact run ledger under $A_STOCK_STATE_HOME/cron/output.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from a_share_rules import latest_trading_day, previous_trading_day
from paths import cron_output_dir
from state_store import atomic_write_json, read_json, update_json_list


ARTIFACT_SCHEMA = "hermes_cron_artifact_v2"
RUN_LEDGER_SCHEMA = "hermes_cron_run_ledger_v2"
ARTIFACT_TEMPLATE = "{cron_output_dir}/{job_id}/{run_id}.json"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def resolve_trading_date(value: date | datetime | str | None = None) -> str:
    if value is None:
        day = latest_trading_day(datetime.now(SHANGHAI))
    elif isinstance(value, str):
        day = latest_trading_day(date.fromisoformat(value[:10]))
    else:
        day = latest_trading_day(value)
    return day.isoformat()


def make_batch_id(trading_date: str) -> str:
    return f"a-share-{trading_date.replace('-', '')}"


def resolve_runtime_name(
    configured: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> str:
    values = env if env is not None else os.environ
    if configured:
        return configured
    if values.get("A_STOCK_RUNTIME"):
        return str(values["A_STOCK_RUNTIME"])
    if values.get("OPENCLAW_HOME"):
        return "openclaw"
    if values.get("HERMES_HOME"):
        return "hermes"
    return "local"


def make_run_id(job_id: str, started_at: Optional[str] = None) -> str:
    started = started_at or now_iso()
    safe_time = started.replace("-", "").replace(":", "").replace("T", "-")[:15]
    return f"{job_id}-{safe_time}-{os.getpid()}"


def artifact_dir(job_id: str) -> str:
    return os.path.join(cron_output_dir(), job_id)


def artifact_path(job_id: str, run_id: str) -> str:
    return os.path.join(artifact_dir(job_id), f"{run_id}.json")


def ledger_path() -> str:
    return os.path.join(cron_output_dir(), "job_runs.json")


def _json_preview(value: str, limit: int = 1200) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def try_parse_json(text: str) -> Optional[Any]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def output_has_signal(parsed: Any, stdout: str) -> bool:
    """Best-effort no-signal detection for silent cron jobs."""
    if not (stdout or "").strip():
        return False
    if not isinstance(parsed, dict):
        return True
    signal_keys = ("alerts", "signals", "events", "factors", "candidates", "confirmations")
    if any(isinstance(parsed.get(key), list) and parsed.get(key) for key in signal_keys):
        return True
    for key in ("alerts", "signals", "events", "factors", "candidates"):
        value = parsed.get(key)
        if isinstance(value, list) and not value:
            return False
    if parsed.get("status") in {"no_signal", "no_events", "empty"}:
        return False
    return True


def summarize_output(parsed: Any, stdout: str) -> Dict[str, Any]:
    if isinstance(parsed, dict):
        summary = {
            "schema": parsed.get("schema"),
            "status": parsed.get("status"),
            "message": parsed.get("message") or parsed.get("summary"),
        }
        for key in ("alerts", "signals", "events", "factors", "candidates", "confirmations"):
            value = parsed.get(key)
            if isinstance(value, list):
                summary[f"{key}_count"] = len(value)
        return {k: v for k, v in summary.items() if v is not None}
    return {"text_preview": _json_preview(stdout, 300)}


def load_latest_artifact(
    job_id: str,
    *,
    trading_date: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    files = sorted(
        glob.glob(os.path.join(artifact_dir(job_id), "*.json")),
        key=os.path.getmtime,
        reverse=True,
    )
    for path in files:
        data = read_json(path, None)
        if not isinstance(data, dict):
            continue
        if trading_date is not None and data.get("trading_date") != trading_date:
            continue
        if batch_id is not None and data.get("batch_id") != batch_id:
            continue
        data.setdefault("artifact_path", path)
        return data
    return None


def _parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


def _dependency_expected_date(trading_date: str, mode: str) -> Optional[str]:
    if mode == "latest":
        return None
    if mode in {"same_trading_date", "same_batch"}:
        return trading_date
    if mode == "previous_trading_day":
        return previous_trading_day(trading_date).isoformat()
    raise ValueError(f"unsupported dependency trading_date mode: {mode}")


def evaluate_dependencies(
    job_ids: Iterable[str],
    *,
    trading_date: str,
    batch_id: str,
    policy: Optional[Dict[str, Any]] = None,
    now: str | datetime | None = None,
) -> Dict[str, Any]:
    """Validate upstream artifacts before the business subprocess can start."""
    policy = dict(policy or {})
    mode = policy.get("trading_date", "same_trading_date")
    optional_jobs = set(policy.get("optional_jobs") or [])
    max_age = policy.get("max_age_minutes")
    accepted_statuses = set(policy.get("accepted_statuses") or ["ok"])
    expected_date = _dependency_expected_date(trading_date, mode)
    current = _parse_datetime(now or now_iso())
    dependencies: List[Dict[str, Any]] = []
    gate_passed = True

    for job_id in job_ids or []:
        required = job_id not in optional_jobs
        artifact = load_latest_artifact(
            job_id,
            trading_date=expected_date,
            batch_id=batch_id if mode == "same_batch" else None,
        )
        reasons: List[str] = []
        if not artifact:
            reasons.append("missing")
            entry: Dict[str, Any] = {"job_id": job_id}
        else:
            entry = {
                "job_id": job_id,
                "run_id": artifact.get("run_id"),
                "batch_id": artifact.get("batch_id"),
                "trading_date": artifact.get("trading_date"),
                "artifact_path": artifact.get("artifact_path"),
                "status": artifact.get("status"),
                "finished_at": artifact.get("finished_at"),
                "summary": artifact.get("summary", {}),
            }
            if artifact.get("status") not in accepted_statuses:
                reasons.append(f"status_{artifact.get('status') or 'missing'}")
            if expected_date is not None and artifact.get("trading_date") != expected_date:
                reasons.append("trading_date_mismatch")
            if mode == "same_batch" and artifact.get("batch_id") != batch_id:
                reasons.append("batch_mismatch")
            if max_age is not None:
                try:
                    age_minutes = (current - _parse_datetime(artifact["finished_at"])).total_seconds() / 60
                    entry["age_minutes"] = round(age_minutes, 3)
                    if age_minutes < 0:
                        reasons.append("future_artifact")
                    elif age_minutes > float(max_age):
                        reasons.append("stale")
                except (KeyError, TypeError, ValueError):
                    reasons.append("invalid_finished_at")

        entry["required"] = required
        entry["reasons"] = reasons
        entry["gate_status"] = "passed" if not reasons else ("blocked" if required else "optional_failed")
        if required and reasons:
            gate_passed = False
        dependencies.append(entry)

    return {
        "passed": gate_passed,
        "trading_date": trading_date,
        "batch_id": batch_id,
        "policy": {
            "trading_date": mode,
            "max_age_minutes": max_age,
            "optional_jobs": sorted(optional_jobs),
            "accepted_statuses": sorted(accepted_statuses),
        },
        "dependencies": dependencies,
    }


def load_context_from(job_ids: Iterable[str]) -> List[Dict[str, Any]]:
    context = []
    for job_id in job_ids or []:
        artifact = load_latest_artifact(job_id)
        if not artifact:
            context.append({"job_id": job_id, "missing": True})
            continue
        context.append({
            "job_id": job_id,
            "run_id": artifact.get("run_id"),
            "artifact_path": artifact.get("artifact_path"),
            "status": artifact.get("status"),
            "finished_at": artifact.get("finished_at"),
            "summary": artifact.get("summary", {}),
        })
    return context


def build_artifact(
    *,
    job: Dict[str, Any],
    run_id: str,
    command: str,
    cwd: str,
    returncode: int,
    stdout: str,
    stderr: str,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    context_artifacts: List[Dict[str, Any]],
    timed_out: bool = False,
    trading_date: Optional[str] = None,
    batch_id: Optional[str] = None,
    dependency_gate: Optional[Dict[str, Any]] = None,
    status_override: Optional[str] = None,
    runtime: Optional[str] = None,
    snapshot_ref: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    parsed = try_parse_json(stdout)
    has_signal = output_has_signal(parsed, stdout)
    status = status_override or ("timeout" if timed_out else ("ok" if returncode == 0 else "failed"))
    return {
        "schema": ARTIFACT_SCHEMA,
        "job_id": job["id"],
        "run_id": run_id,
        "batch_id": batch_id,
        "trading_date": trading_date,
        "context_scope": job.get("context_scope", "cron"),
        "runtime": runtime or os.environ.get("A_STOCK_RUNTIME") or "local",
        "execution_mode": job.get("execution_mode"),
        "deliver": job.get("deliver"),
        "status": status,
        "returncode": returncode,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(duration_seconds, 3),
        "command": command,
        "cwd": cwd,
        "has_signal": has_signal,
        "summary": summarize_output(parsed, stdout),
        "context_from": context_artifacts,
        "dependency_gate": dependency_gate,
        "market_snapshot": snapshot_ref,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_preview": _json_preview(stdout),
        "stderr_preview": _json_preview(stderr),
    }


def write_artifact(artifact: Dict[str, Any]) -> str:
    path = artifact_path(artifact["job_id"], artifact["run_id"])
    artifact["artifact_path"] = path
    atomic_write_json(path, artifact)
    return path


def record_run(artifact: Dict[str, Any], max_items: int = 1000) -> None:
    entry = {
        "schema": RUN_LEDGER_SCHEMA,
        "job_id": artifact["job_id"],
        "run_id": artifact["run_id"],
        "batch_id": artifact.get("batch_id"),
        "trading_date": artifact.get("trading_date"),
        "runtime": artifact.get("runtime"),
        "status": artifact["status"],
        "returncode": artifact["returncode"],
        "started_at": artifact["started_at"],
        "finished_at": artifact["finished_at"],
        "duration_seconds": artifact["duration_seconds"],
        "has_signal": artifact["has_signal"],
        "artifact_path": artifact.get("artifact_path"),
    }
    update_json_list(ledger_path(), entry, unique_key="run_id", max_items=max_items)
