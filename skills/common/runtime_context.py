"""
Hermes runtime context helpers.

Cron jobs must not rely on the active user conversation for intermediate data.
This module gives every job run a stable run id, an artifact path, upstream
context lookup, and a compact run ledger under $HERMES_HOME/cron/output.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from paths import cron_output_dir
from state_store import atomic_write_json, read_json, update_json_list


ARTIFACT_SCHEMA = "hermes_cron_artifact_v1"
RUN_LEDGER_SCHEMA = "hermes_cron_run_ledger_v1"
ARTIFACT_TEMPLATE = "{cron_output_dir}/{job_id}/{run_id}.json"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


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


def load_latest_artifact(job_id: str) -> Optional[Dict[str, Any]]:
    files = sorted(glob.glob(os.path.join(artifact_dir(job_id), "*.json")), key=os.path.getmtime)
    if not files:
        return None
    path = files[-1]
    data = read_json(path, None)
    if isinstance(data, dict):
        data.setdefault("artifact_path", path)
        return data
    return None


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
) -> Dict[str, Any]:
    parsed = try_parse_json(stdout)
    has_signal = output_has_signal(parsed, stdout)
    status = "timeout" if timed_out else ("ok" if returncode == 0 else "failed")
    return {
        "schema": ARTIFACT_SCHEMA,
        "job_id": job["id"],
        "run_id": run_id,
        "context_scope": job.get("context_scope", "cron"),
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
        "status": artifact["status"],
        "returncode": artifact["returncode"],
        "started_at": artifact["started_at"],
        "finished_at": artifact["finished_at"],
        "duration_seconds": artifact["duration_seconds"],
        "has_signal": artifact["has_signal"],
        "artifact_path": artifact.get("artifact_path"),
    }
    update_json_list(ledger_path(), entry, unique_key="run_id", max_items=max_items)
