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
from state_store import atomic_write_json, read_json


ARTIFACT_SCHEMA = "hermes_cron_artifact_v2"
RUN_LEDGER_SCHEMA = "hermes_cron_run_ledger_v2"
ARTIFACT_TEMPLATE = "{cron_output_dir}/{job_id}/{run_id}.json"
SHANGHAI = ZoneInfo("Asia/Shanghai")
# Cap the raw stdout copied into each artifact. The full structured payload is
# preserved in market/snapshots, and summary/has_signal/preview are computed
# from the untruncated stdout, so bounding this field only removes a redundant
# multi-hundred-KB blob from the surface a model can load.
DEFAULT_ARTIFACT_STDOUT_LIMIT = 20000


def _artifact_stdout_limit() -> int:
    raw = os.environ.get("A_STOCK_MAX_ARTIFACT_STDOUT")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_ARTIFACT_STDOUT_LIMIT


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


def ledger_archive_path(month: str | None = None) -> str:
    """Return the append-only archive for evicted run-ledger entries."""
    bucket = str(month or datetime.now(SHANGHAI).strftime("%Y-%m"))[:7]
    return os.path.join(cron_output_dir(), "job_runs_archive", f"{bucket}.jsonl")


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
    status = str(parsed.get("status") or "").lower()
    operational_statuses = {
        "blocked",
        "degraded",
        "error",
        "failed",
        "insufficient_data",
        "stale_data",
        "timeout",
    }
    if status in operational_statuses:
        return True
    # 键名不在白名单里的作业同样要能报告"什么都没做"，否则 has_signal 恒真。
    lists = [value for value in parsed.values() if isinstance(value, list)]
    if lists and not any(lists):
        return False
    # 无信号状态集合：除 no_signal/no_events/empty 外，带否定前缀的变体也要
    # 覆盖（no_new_signal/no_change/no_update/nothing_new），否则 official-
    # policy-watch 这类 "扫描正常但无新增" 的作业会被误判为 has_signal=True，
    # 绕过 silent_when_no_signal 门并把原始 JSON 推到飞书。
    if status in {
        "no_signal",
        "no_events",
        "empty",
        "no_new_signal",
        "no_change",
        "no_update",
        "nothing_new",
    }:
        return False
    return True


SUMMARY_LIST_KEYS = (
    "alerts",
    "signals",
    "events",
    "factors",
    "candidates",
    "confirmations",
)
SUMMARY_BASE_KEYS = {"schema", "status", "message", "summary"}
SUMMARY_MAX_KEYS = 32


def summarize_output(parsed: Any, stdout: str) -> Dict[str, Any]:
    """Reduce a job payload to schema/status plus bounded counters.

    列表只留计数、标量原样带上：既能回答"这次到底做了几件事"，又不会把大块
    载荷搬进 artifact（token 预算）。白名单之外的键名也必须计数——否则
    summary 会塌成只剩 schema，运维面上看到 "ok + has_signal" 却完全无从判断
    作业是干了活还是空转（serenity-refresh-plan 的队列静默积压就是这么漏掉的）。
    """
    if not isinstance(parsed, dict):
        return {"text_preview": _json_preview(stdout, 300)}
    summary = {
        "schema": parsed.get("schema"),
        "status": parsed.get("status"),
        "message": parsed.get("message") or parsed.get("summary"),
    }
    summary = {key: value for key, value in summary.items() if value is not None}
    # 白名单先落位，避免病态载荷把既有口径挤出 SUMMARY_MAX_KEYS。
    for key in SUMMARY_LIST_KEYS:
        value = parsed.get(key)
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
    for key, value in parsed.items():
        if len(summary) >= SUMMARY_MAX_KEYS:
            break
        if key in SUMMARY_BASE_KEYS or key in SUMMARY_LIST_KEYS:
            continue
        if isinstance(value, list):
            summary.setdefault(f"{key}_count", len(value))
        elif isinstance(value, (bool, int, float)):
            summary.setdefault(key, value)
    return summary


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
    calendar_gate: Optional[Dict[str, Any]] = None,
    adaptive_schedule: Optional[Dict[str, Any]] = None,
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    parsed = try_parse_json(stdout)
    usage = parsed.get("usage") if isinstance(parsed, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    has_signal = output_has_signal(parsed, stdout)
    status = status_override or ("timeout" if timed_out else ("ok" if returncode == 0 else "failed"))
    stdout_limit = _artifact_stdout_limit()
    if stdout_limit and len(stdout) > stdout_limit:
        stored_stdout = stdout[:stdout_limit]
        stdout_truncated_chars = len(stdout) - stdout_limit
    else:
        stored_stdout = stdout
        stdout_truncated_chars = 0
    return {
        "schema": ARTIFACT_SCHEMA,
        "job_id": job["id"],
        "run_id": run_id,
        "batch_id": batch_id,
        # Links the artifact to the run-scoped execution trace without giving
        # the trace any ownership of business facts.
        "trace_id": trace_id or os.environ.get("A_STOCK_TRACE_ID") or None,
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
        "calendar_gate": calendar_gate,
        "adaptive_schedule": adaptive_schedule,
        "market_snapshot": snapshot_ref,
        "stdout": stored_stdout,
        "stdout_truncated_chars": stdout_truncated_chars,
        "stderr": stderr,
        "stdout_preview": _json_preview(stdout),
        "stderr_preview": _json_preview(stderr),
        # Command cron jobs normally do not call a model.  Keeping explicit
        # zero/false values makes that fact measurable instead of inferred.
        "llm_called": bool(usage.get("llm_called", False)),
        "agent_turns": int(usage.get("agent_turns") or 0),
        "model": usage.get("model"),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "usage_source": str(usage.get("source") or "deterministic_command"),
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
        "trace_id": artifact.get("trace_id"),
        "trading_date": artifact.get("trading_date"),
        "runtime": artifact.get("runtime"),
        "status": artifact["status"],
        "returncode": artifact["returncode"],
        "started_at": artifact["started_at"],
        "finished_at": artifact["finished_at"],
        "duration_seconds": artifact["duration_seconds"],
        "has_signal": artifact["has_signal"],
        "artifact_path": artifact.get("artifact_path"),
        "llm_called": bool(artifact.get("llm_called", False)),
        "agent_turns": int(artifact.get("agent_turns") or 0),
        "model": artifact.get("model"),
        "input_tokens": int(artifact.get("input_tokens") or 0),
        "output_tokens": int(artifact.get("output_tokens") or 0),
    }

    def _append_and_archive(existing: Any) -> list[dict[str, Any]]:
        rows = [row for row in (existing if isinstance(existing, list) else [])
                if not (isinstance(row, dict) and row.get("run_id") == entry["run_id"])]
        rows.append(entry)
        if max_items and len(rows) > max_items:
            evicted = rows[:-max_items]
            for row in evicted:
                month = str(row.get("finished_at") or row.get("started_at") or "")[:7]
                target = ledger_archive_path(month if len(month) == 7 else None)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            rows = rows[-max_items:]
        return rows

    from state_store import mutate_json
    mutate_json(ledger_path(), _append_and_archive, [])
