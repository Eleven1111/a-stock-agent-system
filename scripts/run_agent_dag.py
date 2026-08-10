#!/usr/bin/env python3
"""Run manifest jobs as a resumable dependency DAG for Hermes or OpenClaw."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(ROOT)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from runtime_context import (  # noqa: E402
    load_latest_artifact,
    make_batch_id,
    resolve_runtime_name,
    resolve_trading_date,
)
from trading_day_gate import evaluate_job_trading_day  # noqa: E402
from paths import hermes_home  # noqa: E402
from state_store import file_lock  # noqa: E402
import execution_trace  # noqa: E402
import feishu_push  # noqa: E402

# 数据质量门控 (quality_gate.py) 延迟导入 — 只在需要时加载
try:
    from quality_gate import (  # type: ignore[import-untyped]  # noqa: E402
        check_dag_run_quality,
        quality_gate_summary,
    )
    _QUALITY_GATE_AVAILABLE = True
except ImportError:
    _QUALITY_GATE_AVAILABLE = False


# Statuses that mean a dependency already reached a terminal failure in this
# batch. A *missing* artifact is deliberately not in this set: on a machine that
# slept through its cron trigger "never ran" is the normal case and must still
# bootstrap.
TERMINAL_FAILURE_STATUSES = frozenset({"failed", "timeout", "error", "blocked"})


def _load_manifest(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _load_artifact(
    job_id: str,
    *,
    trading_date: str,
    batch_id: str,
    env: Mapping[str, str],
) -> dict[str, Any] | None:
    keys = ("A_STOCK_STATE_HOME", "HERMES_HOME")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            value = env.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return load_latest_artifact(
            job_id,
            trading_date=trading_date,
            batch_id=batch_id,
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def execution_order(
    jobs: Mapping[str, Mapping[str, Any]],
    targets: list[str],
) -> list[str]:
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(job_id: str) -> None:
        if job_id in visited:
            return
        if job_id in visiting:
            raise ValueError(f"same-batch dependency cycle at {job_id}")
        if job_id not in jobs:
            raise ValueError(f"unknown DAG job: {job_id}")
        visiting.add(job_id)
        job = jobs[job_id]
        mode = (job.get("dependency_policy") or {}).get("trading_date", "same_trading_date")
        if mode in {"same_trading_date", "same_batch"}:
            optional = set((job.get("dependency_policy") or {}).get("optional_jobs") or [])
            for dependency in job.get("context_from") or []:
                if dependency not in optional:
                    visit(str(dependency))
        visiting.remove(job_id)
        visited.add(job_id)
        ordered.append(job_id)

    for target in targets:
        visit(target)
    return ordered


def _wait_for_run_artifact(
    job_id: str,
    run_id: str,
    *,
    trading_date: str,
    batch_id: str,
    env: Mapping[str, str],
    timeout_seconds: float,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        artifact = _load_artifact(
            job_id,
            trading_date=trading_date,
            batch_id=batch_id,
            env=env,
        )
        if artifact and artifact.get("run_id") == run_id:
            return artifact
        time.sleep(0.1)
    return None


def _apply_quality_gate(
    dag_result: dict[str, Any],
    jobs: dict[str, Any],
    targets: list[str],
) -> dict[str, Any]:
    """对 DAG 结果进行数据质量门控检查并注入 quality_report。"""
    if not _QUALITY_GATE_AVAILABLE or not dag_result.get("runs"):
        return dag_result

    reports: dict[str, dict[str, Any]] = {}
    for run in dag_result["runs"]:
        jid = run.get("job_id", "")
        artifact = run.get("artifact") or {}
        if artifact:
            reports[jid] = check_dag_run_quality(artifact, source_key=jid)
        else:
            reports[jid] = check_dag_run_quality(None, source_key=jid)

    summary = quality_gate_summary(reports)
    dag_result["quality_report"] = summary
    dag_result["_quality_warnings"] = [
        f"{s}: {reports[s]['failures'][0]}"
        for s in summary.get("red", [])
        if reports.get(s, {}).get("failures")
    ]
    return dag_result


def execute_dag(
    *,
    manifest_path: str,
    targets: list[str],
    trading_date: Optional[str] = None,
    batch_id: Optional[str] = None,
    runtime: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    max_attempts: int = 2,
    reuse_targets: bool = False,
) -> dict[str, Any]:
    manifest_path = os.path.abspath(manifest_path)
    manifest = _load_manifest(manifest_path)
    default_day_policy = manifest.get("default_trading_day_policy", "required")
    jobs = {
        job["id"]: {"trading_day_policy": default_day_policy, **job}
        for job in manifest.get("jobs", [])
    }
    run_env = dict(env or os.environ)
    runtime = resolve_runtime_name(runtime, run_env)
    run_env["A_STOCK_RUNTIME"] = runtime
    # One DAG run, one trace id. Every job runner spawned below inherits it, so
    # dependency jobs share the trace while keeping their own run_id.
    run_env[execution_trace.TRACE_ID_ENV] = str(
        execution_trace.resolve_trace_id(run_env) or ""
    )
    calendar_date = (
        str(trading_date)[:10]
        if trading_date
        else datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    )
    active_targets: list[str] = []
    runs: list[dict[str, Any]] = []
    for target in targets:
        if target not in jobs:
            raise ValueError(f"unknown DAG job: {target}")
        gate = evaluate_job_trading_day(jobs[target], calendar_date)
        if gate["action"] == "run":
            active_targets.append(target)
            continue
        command = [
            sys.executable,
            os.path.join(ROOT, "scripts", "agent_job_runner.py"),
            target,
            "--manifest",
            manifest_path,
            "--calendar-date",
            calendar_date,
            "--runtime",
            runtime,
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=run_env,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 78:
            run_status = "blocked_state"
        elif completed.returncode == 75:
            run_status = "blocked_calendar"
        else:
            run_status = (
                "skipped_non_trading_day"
                if gate["action"] == "skip"
                else "blocked_calendar"
            )
        runs.append({
            "job_id": target,
            "status": run_status,
            "returncode": completed.returncode,
            "stderr": completed.stderr[-1000:],
        })

    if not active_targets:
        blocked = any(run["returncode"] != 0 for run in runs)
        result_day = (
            str(trading_date or calendar_date)[:10]
            if blocked
            else resolve_trading_date(trading_date or calendar_date)
        )
        result = {
            "schema": "a_stock_dag_run_v1",
            "status": "blocked" if blocked else "skipped_non_trading_day",
            "runtime": runtime,
            "trading_date": result_day,
            "batch_id": batch_id or make_batch_id(result_day),
            "targets": targets,
            "runs": runs,
        }
        return _apply_quality_gate(result, jobs, targets)

    day = resolve_trading_date(trading_date or calendar_date)
    batch = batch_id or make_batch_id(day)
    order = execution_order(jobs, active_targets)
    target_set = set(active_targets)

    for job_id in order:
        existing = _load_artifact(
            job_id,
            trading_date=day,
            batch_id=batch,
            env=run_env,
        )
        can_reuse = job_id not in target_set or reuse_targets
        if can_reuse and existing and existing.get("status") == "ok":
            runs.append({
                "job_id": job_id,
                "status": "reused",
                "artifact_path": existing.get("artifact_path"),
                "run_id": existing.get("run_id"),
            })
            continue

        # Fail fast on a dependency that already failed today instead of paying
        # for it again: without this, every downstream firing re-ran the same
        # expensive upstream job and multiplied one failure by
        # (firings x attempts x lease waits). ``existing`` is already loaded, so
        # this costs no extra IO, and ``_load_artifact`` is scoped to this
        # trading date. Targets are never short-circuited — a manual rerun of a
        # failed job must stay possible.
        if (
            job_id not in target_set
            and existing
            and existing.get("status") in TERMINAL_FAILURE_STATUSES
        ):
            runs.append({
                "job_id": job_id,
                "status": "blocked",
                "reason": "upstream_failed",
                "upstream_status": existing.get("status"),
                "upstream_run_id": existing.get("run_id"),
                "artifact_path": existing.get("artifact_path"),
            })
            result = {
                "schema": "a_stock_dag_run_v1",
                "status": "blocked",
                "runtime": runtime,
                "trading_date": day,
                "batch_id": batch,
                "targets": targets,
                "runs": runs,
            }
            return _apply_quality_gate(result, jobs, targets)

        command = [
            sys.executable,
            os.path.join(ROOT, "scripts", "agent_job_runner.py"),
            job_id,
            "--manifest",
            manifest_path,
            "--trading-date",
            day,
            "--calendar-date",
            calendar_date,
            "--batch-id",
            batch,
            "--runtime",
            runtime,
        ]
        completed = None
        concurrent_artifact = None
        attempts = max(1, int((jobs[job_id].get("retry_policy") or {}).get("max_attempts", max_attempts)))
        for attempt in range(1, attempts + 1):
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=run_env,
                capture_output=True,
                text=True,
            )
            if completed.returncode == 0:
                break
            if completed.returncode == 76:
                try:
                    duplicate = json.loads(completed.stdout)
                except json.JSONDecodeError:
                    duplicate = {}
                holder_run_id = str((duplicate.get("holder") or {}).get("run_id") or "")
                if holder_run_id:
                    timeout_seconds = float(
                        (jobs[job_id].get("run") or {}).get("timeout_seconds") or 120
                    )
                    concurrent_artifact = _wait_for_run_artifact(
                        job_id,
                        holder_run_id,
                        trading_date=day,
                        batch_id=batch,
                        env=run_env,
                        timeout_seconds=timeout_seconds,
                    )
                    if concurrent_artifact and concurrent_artifact.get("status") == "ok":
                        break
                    # The lease holder has already produced a terminal artifact.
                    # Retrying here would immediately launch the same expensive
                    # job again after its failure/timeout and duplicate the load.
                    if concurrent_artifact:
                        break
        artifact = _load_artifact(
            job_id,
            trading_date=day,
            batch_id=batch,
            env=run_env,
        )
        reused_concurrent = bool(
            concurrent_artifact and concurrent_artifact.get("status") == "ok"
        )
        _BLOCKED_CODES = {75, 78}
        if completed and (completed.returncode == 0 or reused_concurrent):
            status = "ok"
        elif completed and completed.returncode in _BLOCKED_CODES:
            status = "blocked"
        else:
            status = "failed"
        runs.append({
            "job_id": job_id,
            "status": "reused_concurrent" if reused_concurrent else status,
            "attempts": attempt,
            "returncode": completed.returncode if completed else 1,
            "artifact_path": (concurrent_artifact or artifact or {}).get("artifact_path"),
            "stderr": (completed.stderr if completed else "")[-1000:],
        })
        if status != "ok":
            result = {
                "schema": "a_stock_dag_run_v1",
                "status": status,
                "runtime": runtime,
                "trading_date": day,
                "batch_id": batch,
                "targets": targets,
                "runs": runs,
            }
            return _apply_quality_gate(result, jobs, targets)

    result = {
        "schema": "a_stock_dag_run_v1",
        "status": "ok",
        "runtime": runtime,
        "trading_date": day,
        "batch_id": batch,
        "targets": targets,
        "runs": runs,
    }
    return _apply_quality_gate(result, jobs, targets)


def push_telemetry_path() -> str:
    return os.path.join(hermes_home(), "cron", "push_telemetry.jsonl")


def append_push_telemetry(
    record: Mapping[str, Any],
    *,
    telemetry_path: str | None = None,
) -> None:
    path = telemetry_path or push_telemetry_path()
    with file_lock(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record), ensure_ascii=False, default=str))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


def _record_target_output_telemetry(
    job: Mapping[str, Any],
    artifact: Mapping[str, Any],
    *,
    delivered: bool,
    output_chars: int,
    was_compressed: bool,
    silent_reason: str,
    telemetry_path: str | None = None,
) -> None:
    record = {
        "job_id": str(job.get("id") or artifact.get("job_id") or ""),
        "trading_date": str(artifact.get("trading_date") or ""),
        "delivered": bool(delivered),
        "output_chars": int(output_chars),
        "was_compressed": bool(was_compressed),
        "silent_reason": silent_reason,
    }
    try:
        append_push_telemetry(record, telemetry_path=telemetry_path)
    except (OSError, TimeoutError):
        return


def _compress_stdout(job: Mapping[str, Any], artifact: Mapping[str, Any], raw_len: int) -> str:
    """超长 stdout 压成一行摘要。

    各 skill 的 summary 结构不统一，message 可能是 dict/None，直接进 join 会
    TypeError 并让整个作业失败——压缩摘要不值得这个代价，一律按字符串兜底。
    """
    raw_summary = artifact.get("summary")
    summary = raw_summary if isinstance(raw_summary, Mapping) else {}
    raw_message = summary.get("message")
    parts = [
        raw_message if isinstance(raw_message, str) and raw_message
        else f"{job.get('id', 'job')} 运行完成"
    ]
    status = summary.get("status")
    if status and status != "ok":
        parts.append(f"状态={status}")
    counts = {k: v for k, v in summary.items() if k.endswith("_count") and v is not None}
    if counts:
        parts.append(" | ".join(f"{k.replace('_count','')}={v}" for k, v in counts.items()))
    parts.append(f"(输出{raw_len}字符，已压缩)")
    return "\n".join(parts)


def _deliver_feishu_direct(
    job: Mapping[str, Any],
    artifact: Mapping[str, Any],
    *,
    record_telemetry: bool,
    telemetry_path: str | None,
) -> bool:
    """Push to Feishu and report whether the channel accepted the request.

    Acceptance suppresses the fallback delivery; it is not evidence that the
    user received anything, and the trace records it as acceptance only.
    """
    job_id = str(job.get("id") or artifact.get("job_id") or "")
    stdout = str(artifact.get("stdout") or "")
    max_chars = max(200, int(job.get("max_output_chars") or 4000))
    text = feishu_push.render_delivery_text(job_id, stdout, max_chars)
    trace_ctx = {
        "trace_id": execution_trace.resolve_trace_id(create=False),
        "job_id": job_id,
        "run_id": artifact.get("run_id"),
        "batch_id": artifact.get("batch_id"),
        "trading_date": artifact.get("trading_date"),
        "runtime": artifact.get("runtime"),
    }
    execution_trace.delivery_attempted(channel="feishu_direct", **trace_ctx)
    result = feishu_push.push_text(job_id, text)
    status = str(result.get("status") or "unknown")
    execution_trace.delivery_result(status, channel="feishu_direct", **trace_ctx)
    if record_telemetry:
        _record_target_output_telemetry(
            job,
            artifact,
            delivered=status == "sent",
            output_chars=len(text),
            was_compressed=len(stdout) > max_chars,
            silent_reason="none" if status == "sent" else f"feishu_{status}",
            telemetry_path=telemetry_path,
        )
    return status == "sent"


def target_output(
    job: Mapping[str, Any],
    artifact: Mapping[str, Any],
    *,
    record_telemetry: bool = False,
    telemetry_path: str | None = None,
) -> str:
    """Return bounded target output without bypassing delivery/no-signal policy."""
    if job.get("deliver") in {"local", "silent"}:
        if record_telemetry:
            _record_target_output_telemetry(
                job,
                artifact,
                delivered=False,
                output_chars=0,
                was_compressed=False,
                silent_reason="local",
                telemetry_path=telemetry_path,
            )
        return "NO_REPLY\n"
    if job.get("silent_when_no_signal") and not artifact.get("has_signal"):
        if record_telemetry:
            _record_target_output_telemetry(
                job,
                artifact,
                delivered=False,
                output_chars=0,
                was_compressed=False,
                silent_reason="no_signal",
                telemetry_path=telemetry_path,
            )
        return "NO_REPLY\n"
    if job.get("deliver") == "feishu_direct":
        if _deliver_feishu_direct(
            job,
            artifact,
            record_telemetry=record_telemetry,
            telemetry_path=telemetry_path,
        ):
            return "NO_REPLY\n"
        # Fall through to default delivery (OpenClaw announce channel)
    stdout = str(artifact.get("stdout") or "")
    max_chars = max(200, int(job.get("max_output_chars") or 4000))
    was_compressed = len(stdout) > max_chars
    if was_compressed:
        stdout = _compress_stdout(job, artifact, len(stdout))[:max_chars]
    output = stdout + ("\n" if stdout and not stdout.endswith("\n") else "")
    if record_telemetry:
        _record_target_output_telemetry(
            job,
            artifact,
            delivered=True,
            output_chars=len(output),
            was_compressed=was_compressed,
            silent_reason="none",
            telemetry_path=telemetry_path,
        )
    return output


def blocked_notice(targets: list[str], result: Mapping[str, Any]) -> str:
    """One human-readable line explaining why a target never ran.

    A blocked DAG used to print NO_REPLY, which is indistinguishable from "no
    signal today" — the failure was silent by construction on both the announce
    channel and in the dispatch log.
    """
    target = targets[0] if targets else str(result.get("targets") or "")
    culprit = next(
        (
            run
            for run in result.get("runs") or []
            if run.get("status") in {"blocked", "failed", "timeout", "error"}
        ),
        None,
    )
    if not culprit:
        return f"⚠️ {target} 未执行：DAG 状态 {result.get('status')}"
    reason = str(
        culprit.get("reason")
        or culprit.get("upstream_status")
        or culprit.get("status")
    )
    job_id = str(culprit.get("job_id") or "")
    if job_id == target:
        return f"⚠️ {target} 未执行：{reason}"
    return f"⚠️ {target} 未执行：依赖 {job_id} {reason}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="+")
    parser.add_argument("--manifest", default=os.path.join(ROOT, "cron", "hermes-cron-manifest.json"))
    parser.add_argument("--trading-date")
    parser.add_argument("--batch-id")
    parser.add_argument("--runtime", choices=["hermes", "openclaw", "local"])
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--reuse-targets", action="store_true")
    parser.add_argument(
        "--emit-target",
        action="store_true",
        help="Emit the single target job stdout instead of the DAG summary",
    )
    args = parser.parse_args()
    result = execute_dag(
        manifest_path=args.manifest,
        targets=args.targets,
        trading_date=args.trading_date,
        batch_id=args.batch_id,
        runtime=args.runtime,
        max_attempts=args.max_attempts,
        reuse_targets=args.reuse_targets,
    )
    if args.emit_target and result["status"] == "skipped_non_trading_day":
        print("NO_REPLY")
    elif args.emit_target and result["status"] == "blocked":
        print(blocked_notice(args.targets, result))
    elif args.emit_target and result["status"] == "ok" and len(args.targets) == 1:
        artifact = _load_artifact(
            args.targets[0],
            trading_date=result["trading_date"],
            batch_id=result["batch_id"],
            env=os.environ,
        )
        manifest = _load_manifest(args.manifest)
        job = next(
            (item for item in manifest.get("jobs", []) if item.get("id") == args.targets[0]),
            {},
        )
        sys.stdout.write(target_output(job, artifact or {}, record_telemetry=True))
    elif result["status"] in {"ok", "skipped_non_trading_day", "blocked"}:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        # 任务失败时输出人类可读的短消息而不是原始 DAG JSON
        failed_runs = result.get("runs", [])
        failed_jobs = [f"{r['job_id']}(code={r['returncode']})" for r in failed_runs if r.get("status") == "failed"]
        parts = [f"任务失败: {', '.join(failed_jobs)}"] if failed_jobs else [f"DAG运行失败: {result.get('status')}"]
        artifact = (failed_runs or [{}])[0]
        if artifact.get("stderr"):
            err_msg = artifact["stderr"][:200]
            parts.append(f"错误: {err_msg}")
        print("\n".join(parts))
    # blocked is a failure to produce the target, not a quiet day: it must not
    # report success to any exit-code consumer (OpenClaw lastRunStatus today,
    # anything reading the dispatch log tomorrow).
    raise SystemExit(0 if result["status"] in {"ok", "skipped_non_trading_day"} else 1)


if __name__ == "__main__":
    main()
