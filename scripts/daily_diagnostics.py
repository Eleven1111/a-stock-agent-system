#!/usr/bin/env python3
"""每日运行诊断包 —— 把一天的运行情况压成一份可以手动传走的文件。

这个系统跑在两个互相看不见的 Agent 里（OpenClaw 网关 + Hermes 调度器），排障时
证据散落在四五处：execution_trace、每作业 artifact、dispatch 日志、OpenClaw 的
sqlite 台账、以及 /tmp 下随时会被清掉的网关日志。2026-08-05/06 两天的事故复盘
里，光是把时间线拼出来就跨了四个数据源，而且第一轮归因还错了两次。

本脚本不新增任何采集，只做聚合：把已有数据整理成一份带环境指纹的摘要，人可以
直接读，也可以整份贴给别人看。

设计约束（每条都对应一次真实教训）：

- **纯标准库，不 import 任何项目模块**。出事时坏的往往就是项目代码本身，诊断
  工具必须能在项目跑不起来的时候照常工作。
- **输出有界**。生产 execution_trace 单日 6.5MB、artifact 300+ 个，原样传不动；
  这里只出聚合值与截断后的证据摘录。
- **默认脱敏**。产物要经人手传递，日志里可能带 key/token/chat id，一律先洗。
- **环境指纹放第一段**。2026-08-06 的误判全部源于分不清「在看哪台机器、哪个
  版本」——本机 state home 是 ~/.a-stock-agent-cc，另一台是 ~/.hermes，而
  ~/.hermes 在本机恰好是废弃的回退目录。指纹不在最前面，后面的数字都可能被误读。

用法::

    python scripts/daily_diagnostics.py                     # 当天，打到 stdout
    python scripts/daily_diagnostics.py --date 2026-08-06
    python scripts/daily_diagnostics.py --out ~/Desktop/report.md
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
from typing import Any, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

#: 跑到终态且不需要人介入的状态，与 cron_failure_watch 保持同一口径。
HEALTHY = frozenset({
    "ok",
    "duplicate_skipped",
    "skipped_non_trading_day",
    "skipped_adaptive_backoff",
    "success",
})

CHINA_TZ = dt.timezone(dt.timedelta(hours=8))

#: 证据摘录的封顶，避免一条巨大的 traceback 把整份报告撑爆。
STDERR_TAIL_CHARS = 1500
LOG_TAIL_LINES = 40
MAX_EVIDENCE_JOBS = 8

#: 网关侧的错误类别。本仓库不直接调用模型厂商 API（模型回合在 OpenClaw 网关侧），
#: 所以 401/402/EADDRINUSE 只能从网关日志里读；``preopen_preflight`` 复用这一份，
#: 避免两处口径各自漂移。
GATEWAY_PATTERNS = {
    "model_auth": ("401", "unauthorized"),
    "model_balance": ("402", "insufficient balance"),
    "port_conflict": ("eaddrinuse", "address already in use"),
}

#: 脱敏规则。宁可多洗：误洗一个无害字符串只是难读一点，漏洗一个 key 是事故。
_REDACTIONS = (
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(ghp_[A-Za-z0-9]{8,})"),
    re.compile(r"(xox[baprs]-[A-Za-z0-9\-]{8,})"),
    re.compile(r"((?i:bearer)\s+[A-Za-z0-9._\-]{12,})"),
    re.compile(r"((?i:[a-z0-9_]*(?:key|token|secret|password|passwd|chat_id))"
               r"\s*[=:]\s*)([^\s,;\"'}\]]{6,})"),
    re.compile(r"\b([0-9a-fA-F]{32,})\b"),
)


def redact(text: str) -> str:
    """洗掉疑似凭据。产物要过人手传递，这一步不可跳过。"""
    if not text:
        return ""
    out = text
    for pattern in _REDACTIONS:
        if pattern.groups >= 2:
            out = pattern.sub(lambda m: f"{m.group(1)}<REDACTED>", out)
        else:
            out = pattern.sub("<REDACTED>", out)
    return out


def read_jsonl(path: str, needle: Optional[str] = None) -> list[dict[str, Any]]:
    """逐行读 jsonl；needle 命中才解析，避免整份 6.5MB 都进内存。"""
    rows: list[dict[str, Any]] = []
    if not os.path.exists(path):
        return rows
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or (needle and needle not in line):
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return rows
    return rows


def read_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _day_epoch_bounds_ms(day: str) -> tuple[int, int]:
    """Return the half-open Shanghai-local day as epoch milliseconds."""
    start = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(), tzinfo=CHINA_TZ)
    end = start + dt.timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _run(argv: list[str], cwd: Optional[str] = None) -> str:
    try:
        done = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=15, check=False
        )
        return (done.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# --------------------------------------------------------------------------- #
# 1. 环境指纹
# --------------------------------------------------------------------------- #

def section_environment(state_home: str, day: str) -> list[str]:
    head = _run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT)
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT)
    dirty = _run(["git", "status", "--porcelain"], cwd=ROOT)
    behind = _run(["git", "rev-list", "--count", "HEAD..@{u}"], cwd=ROOT)
    sched = _run(["launchctl", "list"])
    running = [line for line in sched.splitlines() if "a-stock" in line or "openclaw" in line]

    lines = [
        "## 1. 环境指纹",
        "",
        "> 读下面任何数字之前先确认这一段：两台机器的 state home 约定相反，"
        "版本不同结论就不同。",
        "",
        f"- 报告日期: `{day}`  (生成于 {dt.datetime.now().isoformat(timespec='seconds')})",
        f"- 主机: `{socket.gethostname()}`",
        f"- 仓库: `{ROOT}`",
        f"- 分支/版本: `{branch or '?'}` @ `{head or '?'}`"
        + (f"  **落后上游 {behind} 个提交**" if behind and behind != "0" else ""),
        f"- 工作区: {'**有未提交改动**' if dirty else '干净'}",
        f"- A_STOCK_STATE_HOME: `{state_home}`"
        + ("" if os.path.isdir(state_home) else "  **该目录不存在**"),
        f"- A_STOCK_RUNTIME: `{os.environ.get('A_STOCK_RUNTIME', '(未设置)')}`",
        f"- A_STOCK_ENV_FILE: `{os.environ.get('A_STOCK_ENV_FILE', '(未设置)')}`",
    ]
    if dirty:
        lines += ["", "未提交改动（前 20 行）：", "```"]
        lines += dirty.splitlines()[:20]
        lines += ["```"]
    lines += ["", "常驻进程：", "```"]
    lines += running or ["(launchctl 中未见 a-stock / openclaw 条目)"]
    lines += ["```", ""]
    return lines


# --------------------------------------------------------------------------- #
# 2. Hermes 侧：trace 聚合
# --------------------------------------------------------------------------- #

def collect_hermes(state_home: str, day: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    trace = read_jsonl(os.path.join(state_home, "cron", "execution_trace.jsonl"), day)
    by_job: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for event in trace:
        by_job[str(event.get("job_id") or "?")].append(event)

    rows = []
    for job, events in sorted(by_job.items()):
        def count(name: str) -> int:
            return sum(1 for e in events if e.get("event_type") == name)

        finished = [e for e in events if e.get("event_type") == "job.finished"]
        statuses = [str(e.get("status")) for e in finished]
        durations = [
            float(e["duration_seconds"])
            for e in finished
            if isinstance(e.get("duration_seconds"), (int, float))
        ]
        unhealthy = sorted({s for s in statuses if s not in HEALTHY})
        claimed = count("dispatch.claimed")
        started = count("job.started")
        rows.append({
            "job_id": job,
            "claimed": claimed,
            "started": started,
            "finished": len(finished),
            "statuses": sorted(set(statuses)),
            "unhealthy": unhealthy,
            "max_duration": max(durations) if durations else None,
            # 触发了却从未启动：可能是依赖感知快速失败短路（PR #162 的正常行为），
            # 也可能是真的没跑起来。两者运维动作不同，这里只如实标注，不臆测。
            "never_started": bool(claimed and not started),
        })

    delivery: collections.Counter = collections.Counter()
    for event in trace:
        etype = str(event.get("event_type") or "")
        if etype.startswith("delivery."):
            delivery[(etype, str(event.get("status")))] += 1

    return rows, {"events": len(trace), "delivery": delivery, "trace_events": trace}


def section_hermes(
    rows: list[dict[str, Any]],
    meta: dict[str, Any],
    executions: Optional[list[dict[str, Any]]] = None,
    execution_evidence: Optional[dict[str, Any]] = None,
) -> list[str]:
    trace_alerts = [r for r in rows if r["unhealthy"] or r["never_started"]]
    execution_alerts = [
        row for row in (executions or []) if row.get("status") != "success"
    ]
    alert_count = len(execution_alerts) if executions is not None else len(trace_alerts)
    if execution_evidence and execution_evidence.get("status") != "ok":
        alert_count += 1
    lines = [
        "## 2. Hermes 侧运行结果",
        "",
        f"当日 trace 事件 {meta['events']} 条，涉及 {len(rows)} 个作业；"
        f"**异常 {alert_count} 个**。",
        "",
    ]
    if executions is not None:
        lines += [
            "### Job Execution Interface（跨来源终态）",
            "",
            "> `success` 必须有业务台账或 execution trace 终态；"
            "OpenClaw 外壳单独报 `ok` 只能得到 `no-evidence`。",
            "",
            "| 作业 | 统一状态 | job ledger | trace 事件 | OpenClaw run | 原因 |",
            "|---|---|---:|---:|---:|---|",
        ]
        for item in executions:
            evidence = item["evidence"]
            lines.append(
                f"| `{item['job_id']}` | {item['status']} | "
                f"{evidence['job_ledger']} | {evidence['execution_trace']} | "
                f"{evidence['openclaw']} | {item['reason']} |"
            )
        if not executions:
            reason = (
                (execution_evidence or {}).get("reason")
                or "当日无可用执行证据"
            )
            lines.append(f"| - | no-evidence | 0 | 0 | 0 | {reason} |")
        lines.append("")

    alerts = trace_alerts
    if alerts:
        lines += ["### 异常作业", "", "| 作业 | 现象 | 最长耗时 |", "|---|---|---|"]
        for row in alerts:
            if row["unhealthy"]:
                symptom = "/".join(row["unhealthy"])
            else:
                symptom = "触发但从未启动（可能是依赖短路，也可能真没跑）"
            dur = f"{row['max_duration']:.1f}s" if row["max_duration"] else "-"
            lines.append(f"| `{row['job_id']}` | {symptom} | {dur} |")
        lines.append("")
    else:
        lines += ["当日无异常作业。", ""]

    lines += [
        "<details><summary>全部作业明细</summary>", "",
        "| 作业 | 触发 | 启动 | 完成 | 状态 | 最长耗时 |", "|---|---:|---:|---:|---|---:|",
    ]
    for row in rows:
        dur = f"{row['max_duration']:.1f}s" if row["max_duration"] else "-"
        lines.append(
            f"| `{row['job_id']}` | {row['claimed']} | {row['started']} | "
            f"{row['finished']} | {','.join(row['statuses']) or '-'} | {dur} |"
        )
    lines += ["", "</details>", ""]

    lines += ["### 推送投递", ""]
    if meta["delivery"]:
        lines += ["| 事件 | 状态 | 次数 |", "|---|---|---:|"]
        for (etype, status), count in sorted(meta["delivery"].items()):
            lines.append(f"| {etype} | {status} | {count} |")
        failed = sum(
            n for (etype, status), n in meta["delivery"].items()
            if etype.endswith("failed") and status == "not_configured"
        )
        if failed:
            lines += [
                "",
                f"> **{failed} 次投递因未配置推送通道失败**：告警生成了但没送达任何人。"
                "配置对应的 chat id 后即可，无需改代码。",
            ]
    else:
        lines.append("当日无投递事件。")
    lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# 3. OpenClaw 侧：sqlite 台账
# --------------------------------------------------------------------------- #

def collect_openclaw(db_path: str, day: str) -> dict[str, Any]:
    """读 OpenClaw 的结构化 run 台账。文本日志只在拿不到它时才退而求其次。"""
    result: dict[str, Any] = {
        "available": False,
        "runs_available": False,
        "path": db_path,
        "runs": [],
        "jobs": [],
    }
    if not os.path.exists(db_path):
        result["error"] = "sqlite 不存在"
        return result
    try:
        # 只读打开：诊断工具绝不能改动被诊断的系统。
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "cron_jobs" in names:
                result["jobs"] = [dict(r) for r in conn.execute("SELECT * FROM cron_jobs")]
                for row in result["jobs"]:
                    row["canonical_job_id"] = _openclaw_canonical_job_id(row)

            if "cron_run_logs" in names:
                column_rows = list(conn.execute("PRAGMA table_info(cron_run_logs)"))
                cols = {str(r[1]): str(r[2] or "") for r in column_rows}
                time_col = next(
                    (c for c in (
                        "started_at", "ts", "run_at_ms", "created_at",
                        "ran_at", "finished_at",
                    )
                     if c in cols),
                    None,
                )
                if time_col:
                    query = "SELECT * FROM cron_run_logs"
                    declared_type = cols[time_col].upper()
                    if "INT" in declared_type:
                        start_ms, end_ms = _day_epoch_bounds_ms(day)
                        maximum = conn.execute(
                            f"SELECT MAX({time_col}) FROM cron_run_logs"
                        ).fetchone()[0]
                        millisecond_scale = not isinstance(maximum, (int, float)) or maximum > 10**11
                        start, end = (
                            (start_ms, end_ms)
                            if millisecond_scale
                            else (start_ms // 1000, end_ms // 1000)
                        )
                        query += f" WHERE {time_col} >= ? AND {time_col} < ? ORDER BY {time_col}"
                        params: tuple[Any, ...] = (start, end)
                        result["time_format"] = (
                            "epoch_milliseconds" if millisecond_scale else "epoch_seconds"
                        )
                    else:
                        query += f" WHERE {time_col} LIKE ? ORDER BY {time_col}"
                        params = (f"{day}%",)
                        result["time_format"] = "text"
                    result["runs"] = [dict(r) for r in conn.execute(query, params)]
                    result["time_col"] = time_col
                    result["runs_available"] = True
                    by_uuid = {
                        str(row.get("job_id")): row.get("canonical_job_id")
                        for row in result["jobs"]
                        if row.get("job_id")
                    }
                    for row in result["runs"]:
                        row["canonical_job_id"] = (
                            by_uuid.get(str(row.get("job_id")))
                            or _openclaw_canonical_job_id(row)
                        )
                else:
                    result["runs_error"] = "cron_run_logs 没有可识别的时间列"
            else:
                result["runs_error"] = "cron_run_logs 表不存在"
            result["available"] = True
    except sqlite3.Error as exc:
        result["error"] = f"sqlite 读取失败: {exc}"
    return result


def _openclaw_canonical_job_id(row: dict[str, Any]) -> Optional[str]:
    """Resolve an OpenClaw UUID row to the manifest job id in its command payload."""
    raw = row.get("payload_message")
    if isinstance(raw, str) and raw:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            argv = payload.get("argv")
            if isinstance(argv, list):
                for index, value in enumerate(argv[:-1]):
                    if str(value).endswith("run_agent_dag.py"):
                        candidate = str(argv[index + 1]).strip()
                        if candidate:
                            return candidate
    name = str(_pick(row, "name", "display_name") or "").strip()
    if name.lower().startswith("a-stock:"):
        return name.split(":", 1)[1].strip() or None
    job_id = str(row.get("job_id") or "").strip()
    if job_id and not re.fullmatch(r"[0-9a-fA-F-]{32,36}", job_id):
        return job_id
    return None


def _pick(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def collect_job_run_ledger(state_home: str, day: str) -> dict[str, Any]:
    """Read the runtime-neutral canonical job ledger for one wall-clock day."""
    path = os.path.join(state_home, "cron", "output", "job_runs.json")
    result: dict[str, Any] = {"available": False, "path": path, "runs": []}
    if not os.path.exists(path):
        result["error"] = "job_runs.json 不存在"
        return result
    payload = read_json(path, None)
    if not isinstance(payload, list):
        result["error"] = "job_runs.json 不是列表"
        return result
    result["available"] = True
    result["runs"] = [
        row for row in payload
        if isinstance(row, dict)
        and str(_pick(row, "started_at", "finished_at") or "")[:10] == day
    ]
    return result


def _execution_status(value: Any) -> str:
    status = str(value or "").strip().lower().replace("_", "-")
    if "timeout" in status or "timed-out" in status:
        return "timeout"
    if status in {item.replace("_", "-") for item in HEALTHY}:
        return "success"
    if status in {"", "running", "started", "claimed", "pending"}:
        return "incomplete"
    return "failed"


def build_job_execution_interface(
    *,
    trace_events: list[dict[str, Any]],
    ledger: dict[str, Any],
    openclaw: dict[str, Any],
) -> list[dict[str, Any]]:
    """Reconcile all available evidence into a fail-closed per-job status.

    The OpenClaw row certifies only that the scheduler command exited.  A
    business ``success`` requires a terminal event from ``job_runs.json`` or
    ``execution_trace``.  Any failure, timeout, or unfinished trace wins over
    a later green scheduler status so the daily report cannot become green by
    looking only at ``lastStatus``.
    """
    source_rows: dict[str, dict[str, list[Any]]] = collections.defaultdict(
        lambda: {"job_ledger": [], "execution_trace": [], "openclaw": []}
    )
    trace_counts: collections.Counter[str] = collections.Counter()
    trace_runs: dict[tuple[str, str], dict[str, bool]] = collections.defaultdict(
        lambda: {"claimed": False, "started": False, "finished": False}
    )

    for row in ledger.get("runs") or []:
        job = str(row.get("job_id") or "").strip()
        if job:
            source_rows[job]["job_ledger"].append(row.get("status"))

    for index, event in enumerate(trace_events):
        job = str(event.get("job_id") or "").strip()
        if not job:
            continue
        event_type = str(event.get("event_type") or "")
        run_id = str(event.get("run_id") or f"event-{index}")
        key = (job, run_id)
        if event_type == "dispatch.claimed":
            trace_runs[key]["claimed"] = True
        elif event_type == "job.started":
            trace_runs[key]["started"] = True
        elif event_type == "job.finished":
            trace_runs[key]["finished"] = True
            source_rows[job]["execution_trace"].append(event.get("status"))
        trace_counts[job] += 1

    for row in openclaw.get("runs") or []:
        job = str(row.get("canonical_job_id") or "").strip()
        if job:
            source_rows[job]["openclaw"].append(
                _pick(row, "status", "state", "result")
            )

    incomplete_jobs = {
        job for (job, _run_id), lifecycle in trace_runs.items()
        if (lifecycle["claimed"] or lifecycle["started"]) and not lifecycle["finished"]
    }
    observations: list[dict[str, Any]] = []
    for job in sorted(source_rows):
        statuses = source_rows[job]
        canonical = [
            _execution_status(value)
            for value in statuses["job_ledger"] + statuses["execution_trace"]
        ]
        scheduler = [_execution_status(value) for value in statuses["openclaw"]]
        combined = canonical + scheduler
        if "timeout" in combined:
            status = "timeout"
            reason = "任一执行证据记录 timeout"
        elif "failed" in combined:
            status = "failed"
            reason = "任一执行证据记录 failed"
        elif "incomplete" in combined or job in incomplete_jobs:
            status = "incomplete"
            reason = "执行证据处于非终态，或 trace 有启动但没有完成"
        elif canonical and all(value == "success" for value in canonical):
            status = "success"
            reason = "业务台账或 execution trace 有健康终态"
        elif scheduler and all(value == "success" for value in scheduler):
            status = "no-evidence"
            reason = "OpenClaw 只证明调度外壳成功，没有业务终态证据"
        else:
            status = "no-evidence"
            reason = "没有可证明业务终态的执行证据"
        observations.append({
            "schema": "job_execution_observation_v1",
            "job_id": job,
            "status": status,
            "source_statuses": {
                name: sorted({str(value) for value in values if value not in (None, "")})
                for name, values in statuses.items()
            },
            "evidence": {
                "job_ledger": len(statuses["job_ledger"]),
                "execution_trace": trace_counts[job],
                "openclaw": len(statuses["openclaw"]),
            },
            "reason": reason,
        })
    return observations


def job_execution_evidence_health(
    *,
    trace_events: list[dict[str, Any]],
    ledger: dict[str, Any],
    openclaw: dict[str, Any],
    executions: list[dict[str, Any]],
) -> dict[str, Any]:
    """State whether the Interface had enough evidence to make any observation."""
    sources = {
        "job_ledger": bool(ledger.get("available")),
        "execution_trace": bool(trace_events),
        "openclaw_runs": bool(openclaw.get("runs_available")),
    }
    return {
        "status": "ok" if executions else "no-evidence",
        "sources": sources,
        "reason": None if executions else "当日没有任何可归属的作业执行证据",
    }


def section_openclaw(data: dict[str, Any]) -> list[str]:
    lines = ["## 3. OpenClaw 侧运行结果", ""]
    if not data["available"]:
        lines += [
            f"未能读取 `{data['path']}`：{data.get('error', '未知原因')}。",
            "",
            "> 若本机不跑 OpenClaw，这一段为空属正常。",
            "",
        ]
        return lines

    if not data.get("runs_available", True):
        lines += [
            f"注册表可读，但当日 run 台账不可用："
            f"{data.get('runs_error', '未知原因')}。",
            "",
        ]
        return lines

    runs = data["runs"]
    lines.append(f"台账 `{data['path']}`，当日 run 记录 **{len(runs)}** 条。")
    lines.append("")
    if not runs:
        lines += ["当日无 run 记录（可能是日期字段口径不同，或当天确未运行）。", ""]
        return lines

    buckets: collections.Counter = collections.Counter()
    failures = []
    for row in runs:
        status = str(_pick(row, "status", "state", "result") or "?")
        buckets[status] += 1
        if status.lower() not in HEALTHY:
            failures.append(row)

    lines += ["| 状态 | 次数 |", "|---|---:|"]
    for status, count in buckets.most_common():
        lines.append(f"| {status} | {count} |")
    lines.append("")

    if failures:
        lines += [f"### 失败的 run（{len(failures)} 条）", "",
                  "| 作业 | 状态 | 耗时 | 投递 | 错误摘要 |", "|---|---|---|---|---|"]
        for row in failures[:25]:
            job = str(_pick(row, "job_id", "name", "cron_job_id") or "?")
            status = str(_pick(row, "status", "state") or "?")
            dur = _pick(row, "duration_ms", "duration", "elapsed_ms")
            dur_txt = f"{float(dur) / 1000:.1f}s" if isinstance(dur, (int, float)) else "-"
            delivery = str(_pick(row, "delivery_status", "delivery") or "-")
            err = str(_pick(row, "error", "summary", "diagnostics") or "")
            err = redact(err).replace("|", "\\|").replace("\n", " ")[:160]
            lines.append(f"| `{job}` | {status} | {dur_txt} | {delivery} | {err} |")
        lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# 4. 注册漂移：manifest 与 OpenClaw 注册表比对
# --------------------------------------------------------------------------- #

#: 一条注册记录里，作业 id 可能藏在哪些列。只取第一个非空字段是不够的：
#: OpenClaw 的 ``cron_jobs`` 用 UUID 做主键，人类可读的名字和真正的命令行在
#: 别的列里，于是每一条注册都被判成对不上 —— 2026-08-19 报告里
#: 「52 enabled 全部 missing / 61 注册全部 extra」就是这个形态（issue #245）。
#: ``command`` 尤其可靠：注册项跑的是 ``run_agent_dag.py <job-id>``。
IDENTITY_FIELDS = (
    "job_id", "name", "id", "title", "slug", "task",
    "command", "command_argv", "argv", "script",
)


def _identity_values(row: dict[str, Any]) -> list[str]:
    """这条注册记录里所有可能写着作业 id 的字符串。"""
    return [
        str(row[field])
        for field in IDENTITY_FIELDS
        if field in row and row[field] not in (None, "")
    ]


def _registration_label(row: dict[str, Any]) -> str:
    """报告里怎么称呼这条注册 —— 优先人类可读的名字，UUID 兜底。"""
    return str(_pick(row, "name", "title", "job_id", "id") or "<unnamed>")


def collect_drift(manifest_path: str, openclaw: dict[str, Any]) -> dict[str, Any]:
    """结构化的注册漂移结论。

    manifest 改了但注册没跟上，只在执行时才炸 —— 这正是 issue #142 的形态。
    本函数只出结论不出版式，因为它有两个消费者：日报（23:10，人读）与开盘前
    体检（08:05，机器判 red/green）。渲染留在 ``section_drift``。
    """
    result: dict[str, Any] = {
        "schema": "job_registration_drift_v1",
        "status": "unavailable",
        "manifest_path": manifest_path,
        "enabled_count": 0,
        "registered_count": None,
        "missing": [],
        "extra": [],
        "reason": None,
        # 哪一步读不到 —— 渲染与体检都按这个分叉，不靠 reason 文案做判断
        "unavailable_at": None,
    }
    manifest = read_json(manifest_path, {})
    jobs = manifest.get("jobs") or []
    if not jobs:
        result["unavailable_at"] = "manifest"
        result["reason"] = f"未能读取 manifest `{manifest_path}`。"
        return result

    enabled = {str(j.get("id")) for j in jobs if j.get("enabled")}
    result["enabled_count"] = len(enabled)

    if not openclaw.get("available") or not openclaw.get("jobs"):
        result["unavailable_at"] = "openclaw"
        result["reason"] = "未能读取 OpenClaw 注册表，跳过比对。"
        return result

    rows = openclaw["jobs"]
    result["registered_count"] = len(rows)

    # OpenClaw 的作业名可能带前缀（如 "A-stock: xxx"），只做包含式匹配，
    # 匹配不上的列出来让人判断，不自作主张认定为漂移。
    matched: set[str] = set()
    unmatched: list[str] = []
    for row in rows:
        haystacks = _identity_values(row)
        hits = {job for job in enabled if any(job in text for text in haystacks)}
        if hits:
            matched |= hits
        else:
            unmatched.append(_registration_label(row))

    if rows and enabled and not matched:
        # 全量对不上：与其说 52 个作业真的都没注册，不如说这一列根本不是
        # 作业名（OpenClaw 主键是 UUID 时就是这个形态）。用空集去支撑一个
        # 强结论，是这套诊断最该避免的错法 —— 所以报「测不准」而不是报漂移。
        result["unavailable_at"] = "key_mismatch"
        result["reason"] = (
            f"{len(rows)} 条注册记录没有一条能对上 manifest 的作业 id，"
            f"疑似比对键不是作业名（如注册表主键为 UUID），"
            f"而不是 {len(enabled)} 个作业都未注册。请核对 cron_jobs 的列名。"
        )
        return result

    result["missing"] = sorted(enabled - matched)
    result["extra"] = sorted(set(unmatched))
    result["status"] = "drift" if (result["missing"] or result["extra"]) else "ok"
    return result


def section_drift(manifest_path: str, openclaw: dict[str, Any]) -> list[str]:
    """把 :func:`collect_drift` 的结论渲染成日报的一节。"""
    lines = ["## 4. 作业注册漂移", ""]
    drift = collect_drift(manifest_path, openclaw)
    if drift["unavailable_at"] == "manifest":
        return lines + [str(drift["reason"]), ""]
    lines.append(f"manifest enabled 作业 **{drift['enabled_count']}** 个。")
    if drift["unavailable_at"] == "openclaw":
        return lines + ["", str(drift["reason"]), ""]

    lines.append(f"OpenClaw 注册 **{drift['registered_count']}** 个。")
    if drift["unavailable_at"]:
        # 任何「测不准」都必须把理由印出来。落到下面的漂移版式会渲染出
        # 空的 missing/extra 再什么都不说 —— 那比报错还糟。
        return lines + ["", str(drift["reason"]), ""]
    lines.append("")
    if drift["missing"]:
        lines += ["**manifest 里 enabled 但 OpenClaw 未注册**（改了没同步注册？）：", ""]
        lines += [f"- `{job}`" for job in drift["missing"]] + [""]
    if drift["extra"]:
        lines += ["**OpenClaw 注册了但不在 manifest enabled 列表**"
                  "（非仓库作业，或 manifest 已下线）：", ""]
        lines += [f"- `{job}`" for job in drift["extra"]] + [""]
    if drift["status"] == "ok":
        lines += ["两边一致，无漂移。", ""]
    return lines


# --------------------------------------------------------------------------- #
# 5. 证据摘录
# --------------------------------------------------------------------------- #

def section_evidence(state_home: str, day: str, rows: list[dict[str, Any]]) -> list[str]:
    lines = ["## 5. 证据摘录（已脱敏）", ""]
    bad = [r for r in rows if r["unhealthy"]][:MAX_EVIDENCE_JOBS]
    if not bad:
        lines += ["当日无失败作业，无需摘录。", ""]
    for row in bad:
        job = row["job_id"]
        folder = os.path.join(state_home, "cron", "output", job)
        stamp = day.replace("-", "")
        try:
            files = sorted(
                (os.path.join(folder, f) for f in os.listdir(folder)
                 if stamp in f and f.endswith(".json")),
                key=os.path.getmtime,
                reverse=True,
            )
        except OSError:
            files = []
        lines += [f"### `{job}`", ""]
        if not files:
            lines += ["当日无 artifact 落盘 —— 作业可能根本没跑，或 runner 在写盘前就崩了。", ""]
            continue
        artifact = read_json(files[0], {})
        lines += [
            f"- artifact: `{os.path.basename(files[0])}`",
            f"- status: `{artifact.get('status')}`  returncode: `{artifact.get('returncode')}`"
            f"  duration: `{artifact.get('duration_seconds')}`",
        ]
        stderr = str(artifact.get("stderr") or "")
        if stderr:
            lines += ["", "stderr 尾部：", "```"]
            lines += [redact(stderr[-STDERR_TAIL_CHARS:])]
            lines += ["```"]
        lines.append("")
    return lines


def section_gateway_log(log_dir: str, day: str) -> list[str]:
    lines = ["## 6. OpenClaw 网关日志错误行（已脱敏）", ""]
    candidates = [
        os.path.join(log_dir, f"openclaw-{day}.log"),
        os.path.join(log_dir, "openclaw.log"),
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if not path:
        lines += [
            f"未找到网关日志（查过 `{log_dir}`）。",
            "",
            "> 默认路径 `/tmp/openclaw/` **重启即清空**，事故后往往已无证据。"
            "建议在 OpenClaw 配置里把 `logging.file` 改到 `~/.openclaw/logs/`。",
            "",
        ]
        return lines
    hits: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                low = line.lower()
                if "error" in low or "failed" in low or "timed out" in low:
                    hits.append(line.rstrip())
    except OSError as exc:
        return lines + [f"读取 `{path}` 失败：{exc}", ""]
    lines.append(f"`{path}`：匹配到 {len(hits)} 行，取最后 {LOG_TAIL_LINES} 行。")
    lines += ["", "```"]
    lines += [redact(line) for line in hits[-LOG_TAIL_LINES:]] or ["(无)"]
    lines += ["```", ""]
    return lines


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 结构化：单日 findings 与跨天聚合
# --------------------------------------------------------------------------- #

def collect_gateway_errors(log_dir: str, day: str) -> dict[str, Any]:
    """按类别数网关日志里的认证/余额/端口错误。

    ``preopen_preflight`` 与本模块的第 6 节共用这一份口径。日志默认在
    ``/tmp/openclaw``，重启即清空 —— 读不到是 ``unavailable``，不是"没有错误"。
    """
    candidates = [
        os.path.join(log_dir, f"openclaw-{day}.log"),
        os.path.join(log_dir, "openclaw.log"),
    ]
    path = next((item for item in candidates if os.path.exists(item)), None)
    result: dict[str, Any] = {
        "status": "unavailable",
        "log_dir": log_dir,
        "log_path": path,
        "counts": {name: 0 for name in GATEWAY_PATTERNS},
        "samples": {},
        "reason": None,
    }
    if path is None:
        result["reason"] = f"未找到网关日志（查过 `{log_dir}`）"
        return result
    samples: dict[str, list[str]] = {name: [] for name in GATEWAY_PATTERNS}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                low = line.lower()
                for name, needles in GATEWAY_PATTERNS.items():
                    if any(needle in low for needle in needles):
                        result["counts"][name] += 1
                        if len(samples[name]) < 2:
                            samples[name].append(redact(line.strip())[:200])
    except OSError as exc:
        result["reason"] = f"读取 `{path}` 失败：{exc}"
        return result
    result["status"] = "ok"
    result["samples"] = {
        name: rows for name, rows in samples.items() if result["counts"][name]
    }
    return result


def required_dependents(manifest_path: str) -> dict[str, list[str]]:
    """每个作业被哪些 **enabled 且未把它标为 optional** 的作业依赖。

    严重度分级不该靠拍脑袋写死一张作业清单：一个作业有没有下游硬依赖，
    manifest 里本来就写着。有硬依赖 = 它挂了会连锁，按 P0；没有 = P1。
    """
    manifest = read_json(manifest_path, {}) or {}
    dependents: dict[str, list[str]] = {}
    for job in manifest.get("jobs") or []:
        if not job.get("enabled"):
            continue
        policy = job.get("dependency_policy") or {}
        optional = {str(item) for item in (policy.get("optional_jobs") or [])}
        for upstream in job.get("context_from") or []:
            if str(upstream) in optional:
                continue
            dependents.setdefault(str(upstream), []).append(str(job.get("id")))
    return {key: sorted(value) for key, value in dependents.items()}


def _job_severity(job_id: str, dependents: dict[str, list[str]]) -> str:
    return "P0" if dependents.get(job_id) else "P1"


def _job_findings(
    rows: list[dict[str, Any]], dependents: dict[str, list[str]]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for row in rows:
        job = str(row["job_id"])
        for status in row["unhealthy"]:
            findings.append({
                "key": f"job_unhealthy:{job}:{status}",
                "kind": "job_unhealthy",
                "subject": job,
                "severity": _job_severity(job, dependents),
                "detail": f"作业 {job} 当日出现 {status}",
                "count": 1,
                "downstream": dependents.get(job, []),
            })
        if row["never_started"]:
            findings.append({
                "key": f"job_never_started:{job}",
                "kind": "job_never_started",
                "subject": job,
                "severity": _job_severity(job, dependents),
                "detail": f"作业 {job} 被触发 {row['claimed']} 次但从未启动",
                "count": row["claimed"],
                "downstream": dependents.get(job, []),
            })
    return findings


def _delivery_findings(meta: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for (etype, status), count in (meta.get("delivery") or {}).items():
        if not str(etype).endswith("failed"):
            continue
        findings.append({
            "key": f"delivery_failed:{status}",
            "kind": "delivery_failed",
            "subject": str(status),
            # 告警生成了却没送达任何人 —— 与"没有告警"在事后完全无法区分
            "severity": "P1",
            "detail": f"{count} 次投递失败（{status}）",
            "count": int(count),
        })
    return findings


def _openclaw_findings(openclaw: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for row in openclaw.get("runs") or []:
        status = str(_pick(row, "status", "state", "result") or "?")
        if status.lower() in HEALTHY:
            continue
        job = str(_pick(row, "job_id", "name", "cron_job_id") or "?")
        findings.append({
            "key": f"openclaw_run_failed:{job}:{status}",
            "kind": "openclaw_run_failed",
            "subject": job,
            "severity": "P2",
            "detail": f"OpenClaw run {job} 状态 {status}",
            "count": 1,
        })
    return findings


def _drift_findings(drift: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for job in drift.get("missing") or []:
        findings.append({
            "key": f"registration_missing:{job}",
            "kind": "registration_missing",
            "subject": str(job),
            # manifest 里 enabled 却没注册 = 它每天都在"应该跑"但从来没跑
            "severity": "P0",
            "detail": f"{job} 在 manifest 里 enabled，但 OpenClaw 未注册",
            "count": 1,
        })
    for job in drift.get("extra") or []:
        findings.append({
            "key": f"registration_extra:{job}",
            "kind": "registration_extra",
            "subject": str(job),
            "severity": "P2",
            "detail": f"{job} 已注册但不在 manifest enabled 列表",
            "count": 1,
        })
    return findings


def _gateway_findings(gateway: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for name, count in (gateway.get("counts") or {}).items():
        if not count:
            continue
        findings.append({
            "key": f"gateway_{name}",
            "kind": "gateway_error",
            "subject": name,
            "severity": "P1",
            "detail": f"网关日志出现 {name} × {count}",
            "count": int(count),
        })
    return findings


def _execution_findings(
    executions: list[dict[str, Any]], dependents: dict[str, list[str]]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for row in executions:
        status = str(row.get("status") or "no-evidence")
        if status == "success":
            continue
        job = str(row.get("job_id") or "?")
        if status in {"timeout", "failed"}:
            key = f"job_unhealthy:{job}:{status}"
            kind = "job_unhealthy"
        elif status == "incomplete":
            key = f"job_incomplete:{job}"
            kind = "job_incomplete"
        else:
            key = f"job_no_evidence:{job}"
            kind = "job_no_evidence"
        findings.append({
            "key": key,
            "kind": kind,
            "subject": job,
            "severity": _job_severity(job, dependents),
            "detail": f"作业 {job} 统一执行状态为 {status}：{row.get('reason')}",
            "count": 1,
            "downstream": dependents.get(job, []),
        })
    return findings


def collect_findings(
    *,
    rows: list[dict[str, Any]],
    meta: dict[str, Any],
    openclaw: dict[str, Any],
    drift: dict[str, Any],
    gateway: dict[str, Any],
    dependents: dict[str, list[str]],
    executions: Optional[list[dict[str, Any]]] = None,
    execution_evidence: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """把当天各来源的异常压成一组带稳定 key 的 finding。

    key 必须跨天稳定，否则聚合会把同一个问题算成每天都是新问题。
    因此 key 只由「类别 + 主体 + 现象」构成，不含日期、run_id、耗时。
    """
    job_findings = (
        _execution_findings(executions, dependents)
        if executions is not None
        else [*_job_findings(rows, dependents), *_openclaw_findings(openclaw)]
    )
    findings = [
        *job_findings,
        *_delivery_findings(meta),
        *_drift_findings(drift),
        *_gateway_findings(gateway),
    ]
    if execution_evidence and execution_evidence.get("status") != "ok":
        findings.append({
            "key": "job_execution_evidence:no-evidence",
            "kind": "job_execution_no_evidence",
            "subject": "job-execution-interface",
            "severity": "P1",
            "detail": str(execution_evidence.get("reason") or "执行证据不可用"),
            "count": 1,
        })
    merged: dict[str, dict[str, Any]] = {}
    for finding in findings:
        existing = merged.get(finding["key"])
        if existing is None:
            merged[finding["key"]] = finding
        else:
            existing["count"] += finding["count"]
    return sorted(
        merged.values(), key=lambda item: (item["severity"], item["key"])
    )


def collect_all(
    day: str, state_home: str, openclaw_db: str, log_dir: str
) -> dict[str, Any]:
    """一次采集，Markdown 与结构化两份产物共用。

    归档模式两份都要出。分别采集会把 6.5MB 的 execution_trace 与 OpenClaw sqlite
    各读两遍，而这个作业只有 60s 的 short 档预算。
    """
    manifest_path = os.path.join(ROOT, "cron", "hermes-cron-manifest.json")
    rows, meta = collect_hermes(state_home, day)
    openclaw = collect_openclaw(openclaw_db, day)
    ledger = collect_job_run_ledger(state_home, day)
    executions = build_job_execution_interface(
        trace_events=meta.get("trace_events") or [],
        ledger=ledger,
        openclaw=openclaw,
    )
    execution_evidence = job_execution_evidence_health(
        trace_events=meta.get("trace_events") or [],
        ledger=ledger,
        openclaw=openclaw,
        executions=executions,
    )
    return {
        "day": day,
        "state_home": state_home,
        "log_dir": log_dir,
        "manifest_path": manifest_path,
        "rows": rows,
        "meta": meta,
        "openclaw": openclaw,
        "job_ledger": ledger,
        "executions": executions,
        "execution_evidence": execution_evidence,
        "drift": collect_drift(manifest_path, openclaw),
        "gateway": collect_gateway_errors(log_dir, day),
        "dependents": required_dependents(manifest_path),
    }


def build_structured_report(
    day: str,
    state_home: str,
    openclaw_db: str,
    log_dir: str,
    collected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """当天的机器可读诊断结论。跨天聚合读的就是这一份。"""
    bundle = collected or collect_all(day, state_home, openclaw_db, log_dir)
    rows, meta = bundle["rows"], bundle["meta"]
    openclaw, drift = bundle["openclaw"], bundle["drift"]
    gateway, dependents = bundle["gateway"], bundle["dependents"]
    executions = bundle.get("executions")
    findings = collect_findings(
        rows=rows, meta=meta, openclaw=openclaw,
        drift=drift, gateway=gateway, dependents=dependents,
        executions=executions,
        execution_evidence=bundle.get("execution_evidence"),
    )
    severity_counts = collections.Counter(item["severity"] for item in findings)
    return {
        "schema": "a_stock_diagnostics_daily_v1",
        "date": day,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "state_home": state_home,
        # 「这一天到底观测到了哪些作业」—— 聚合判定「已修复」时必须用它兜底：
        # 一个问题消失，可能是修好了，也可能是那个作业当天压根没跑。
        "observed_subjects": sorted(
            {
                str(row["job_id"])
                for row in (executions or [])
                if row.get("status") == "success"
            }
            if executions is not None
            else {str(row["job_id"]) for row in rows}
        ),
        "trace_events": int(meta.get("events") or 0),
        "openclaw_available": bool(openclaw.get("available")),
        "openclaw_runs_available": bool(openclaw.get("runs_available")),
        "job_ledger_available": bool(bundle.get("job_ledger", {}).get("available")),
        "job_execution_status_counts": dict(collections.Counter(
            str(row.get("status")) for row in (executions or [])
        )),
        "job_executions": executions or [],
        "job_execution_evidence": bundle.get("execution_evidence") or {
            "status": "unavailable"
        },
        "drift_status": drift.get("status"),
        "gateway_status": gateway.get("status"),
        "severity_counts": {
            level: severity_counts.get(level, 0) for level in ("P0", "P1", "P2")
        },
        "finding_count": len(findings),
        "findings": findings,
    }


_DAILY_JSON_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def load_daily_reports(out_dir: str, days: int, today: str) -> list[dict[str, Any]]:
    """读归档目录里最近 *days* 天的结构化日报，按日期升序。"""
    if not os.path.isdir(out_dir):
        return []
    try:
        cutoff = dt.date.fromisoformat(today) - dt.timedelta(days=days - 1)
    except ValueError:
        return []
    reports = []
    for name in sorted(os.listdir(out_dir)):
        match = _DAILY_JSON_RE.match(name)
        if not match:
            continue
        try:
            stamp = dt.date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if not (cutoff <= stamp <= dt.date.fromisoformat(today)):
            continue
        value = read_json(os.path.join(out_dir, name), None)
        if isinstance(value, dict) and value.get("findings") is not None:
            reports.append(value)
    return sorted(reports, key=lambda item: str(item.get("date") or ""))


def _index_findings(reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 key 把多天的 finding 折叠成一条，带 first_seen / last_seen / occurrences。"""
    seen: dict[str, dict[str, Any]] = {}
    for report in reports:
        date = str(report.get("date") or "")
        for finding in report.get("findings") or []:
            key = str(finding.get("key"))
            entry = seen.setdefault(key, {
                "key": key,
                "kind": finding.get("kind"),
                "subject": finding.get("subject"),
                "severity": finding.get("severity"),
                "detail": finding.get("detail"),
                "first_seen": date,
                "last_seen": date,
                "occurrences": 0,
                "days": [],
            })
            entry["last_seen"] = date
            entry["severity"] = finding.get("severity") or entry["severity"]
            entry["detail"] = finding.get("detail") or entry["detail"]
            entry["occurrences"] += 1
            entry["days"].append(date)
    return seen


def _classify(
    entry: dict[str, Any],
    *,
    latest_keys: set[str],
    observed: set[str],
    latest_date: str,
) -> str:
    """把一条 finding 归入 new / recurring / resolved / unverified。

    **「消失」不等于「修好」**：不再出现可能是修好了，也可能是那个作业当天压根
    没跑。只有主体在最后一天确实被观测到，才算 ``resolved``。
    """
    if entry["key"] in latest_keys:
        return "new" if entry["occurrences"] == 1 else "recurring"
    if not entry["subject"] or str(entry["subject"]) in observed:
        return "resolved"
    entry["unverified_reason"] = (
        f"{entry['subject']} 在 {latest_date} 未被观测到，消失可能只是因为它没运行"
    )
    return "unverified"


def build_rollup(reports: list[dict[str, Any]], *, days: int) -> dict[str, Any]:
    """跨天聚合：区分新问题 / 重复问题 / 已验证修复。

    issue #239 的验收标准里，「已验证修复」这四个字是关键 —— 分类口径见
    :func:`_classify`，空集不得被当成通过。
    """
    if not reports:
        return {
            "schema": "a_stock_diagnostics_rollup_v1",
            "status": "insufficient_data",
            "reason": "归档目录里没有结构化日报，无法聚合",
            "window": {"days": days, "reports_found": 0},
            "counts": {"new": 0, "recurring": 0, "resolved": 0, "unverified": 0},
            "new": [], "recurring": [], "resolved": [], "unverified": [],
        }

    latest = reports[-1]
    latest_keys = {str(item["key"]) for item in latest.get("findings") or []}
    observed = set(latest.get("observed_subjects") or [])
    seen = _index_findings(reports)

    buckets: dict[str, list[dict[str, Any]]] = {
        "new": [], "recurring": [], "resolved": [], "unverified": []
    }
    for entry in seen.values():
        bucket = _classify(
            entry,
            latest_keys=latest_keys,
            observed=observed,
            latest_date=str(latest.get("date") or ""),
        )
        entry["fix_status"] = bucket
        buckets[bucket].append(entry)

    for name in buckets:
        buckets[name].sort(key=lambda item: (item["severity"], item["key"]))

    return {
        "schema": "a_stock_diagnostics_rollup_v1",
        # 样本不足要显式说出来，不能让 5 天的标准被 1 份报告糊过去
        "status": "ok" if len(reports) >= days else "partial",
        "reason": None if len(reports) >= days else (
            f"窗口要求 {days} 天，实际只有 {len(reports)} 份结构化日报"
        ),
        "window": {
            "days": days,
            "reports_found": len(reports),
            "from": reports[0].get("date"),
            "to": latest.get("date"),
        },
        "severity_counts": {
            level: sum(
                1 for entry in seen.values()
                if entry["severity"] == level and entry["fix_status"] != "resolved"
            )
            for level in ("P0", "P1", "P2")
        },
        "counts": {name: len(rows) for name, rows in buckets.items()},
        **buckets,
    }


def build_report(
    day: str,
    state_home: str,
    openclaw_db: str,
    log_dir: str,
    collected: dict[str, Any] | None = None,
) -> str:
    bundle = collected or collect_all(day, state_home, openclaw_db, log_dir)
    rows, meta = bundle["rows"], bundle["meta"]
    openclaw = bundle["openclaw"]
    manifest_path = bundle["manifest_path"]

    out: list[str] = [f"# A股系统每日运行诊断 · {day}", ""]
    out += section_environment(state_home, day)
    out += section_hermes(
        rows,
        meta,
        bundle.get("executions"),
        bundle.get("execution_evidence"),
    )
    out += section_openclaw(openclaw)
    out += section_drift(manifest_path, openclaw)
    out += section_evidence(state_home, day, rows)
    out += section_gateway_log(log_dir, day)
    out += [
        "---",
        "",
        "本报告由 `scripts/daily_diagnostics.py` 生成，只聚合既有数据、不新增采集。",
        "凭据已按规则脱敏，但**传出前请自行再扫一眼**。",
    ]
    return "\n".join(out) + "\n"


_REPORT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(?:md|json)$")


def prune_reports(out_dir: str, retention_days: int, today: str) -> list[str]:
    """删掉超出保留窗口的历史报告，返回被删的文件名。

    只认 ``YYYY-MM-DD.md`` / ``YYYY-MM-DD.json`` 两种命名，且只按文件名里的日期
    判断——不看 mtime。手工另存的报告（如 `2026-08-06-incident.md`）因此不会被
    误删；重跑历史某天也不会因为 mtime 是今天就躲过清理。
    """
    removed: list[str] = []
    if retention_days <= 0 or not os.path.isdir(out_dir):
        return removed
    try:
        cutoff = dt.date.fromisoformat(today) - dt.timedelta(days=retention_days)
    except ValueError:
        return removed
    for name in sorted(os.listdir(out_dir)):
        matched = _REPORT_RE.match(name)
        if not matched:
            continue
        try:
            stamp = dt.date.fromisoformat(matched.group(1))
        except ValueError:
            continue
        if stamp < cutoff:
            try:
                os.remove(os.path.join(out_dir, name))
                removed.append(name)
            except OSError:
                continue
    return removed


def count_alerts(report: str) -> int:
    matched = re.search(r"\*\*异常 (\d+) 个\*\*", report)
    return int(matched.group(1)) if matched else 0


def _archive(
    args: argparse.Namespace,
    *,
    report: str,
    collected: dict[str, Any],
    out_dir: str,
) -> str:
    """定时归档：报告本体落盘，回给调度器的只有一行摘要。

    事故当天再去翻证据往往已经没了（OpenClaw 主日志在 /tmp，重启即清空），
    所以每天先存一份，是这个模式存在的全部理由。
    """
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, f"{args.date}.md")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(report)
    # 同时落一份结构化的：跨天聚合读它，Markdown 那份是给人看的。
    # 「连续 5 个交易日的结构化摘要」这条验收标准，缺了这一步就无从谈起。
    structured = build_structured_report(
        args.date,
        os.path.expanduser(args.state_home),
        os.path.expanduser(args.openclaw_db),
        os.path.expanduser(args.openclaw_log_dir),
        collected=collected,
    )
    with open(os.path.join(out_dir, f"{args.date}.json"), "w", encoding="utf-8") as handle:
        json.dump(structured, handle, ensure_ascii=False, indent=2)
    removed = prune_reports(out_dir, args.retention_days, args.date)
    counts = structured["severity_counts"]
    summary = (
        f"诊断报告 {args.date}：异常 {count_alerts(report)} 项"
        f"（P0 {counts['P0']} / P1 {counts['P1']} / P2 {counts['P2']}），"
        f"{os.path.getsize(target) / 1024:.1f} KB → {target}"
    )
    # 每天回一行「近 N 日新增/重复/已修」——「问题有没有真的收敛」只能跨天看，
    # 单日报告永远回答不了。跑完就有，不必再单开一个作业。
    rollup = build_rollup(
        load_daily_reports(out_dir, args.days, args.date), days=args.days
    )
    if rollup["status"] != "insufficient_data":
        rc = rollup["counts"]
        summary += (
            f"；近 {rollup['window']['reports_found']} 日 新增 {rc['new']} /"
            f" 重复 {rc['recurring']} / 已修 {rc['resolved']}"
        )
        if rc["unverified"]:
            summary += f" / 待验证 {rc['unverified']}"
    if removed:
        summary += f"；清理过期报告 {len(removed)} 份"
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="每日运行诊断包（跨 OpenClaw / Hermes）")
    parser.add_argument("--date", default=dt.date.today().isoformat(), help="默认今天")
    parser.add_argument(
        "--state-home",
        default=os.environ.get("A_STOCK_STATE_HOME", os.path.expanduser("~/.hermes")),
    )
    parser.add_argument(
        "--openclaw-db",
        default=os.path.expanduser("~/.openclaw/state/openclaw.sqlite"),
    )
    parser.add_argument("--openclaw-log-dir", default="/tmp/openclaw")
    parser.add_argument("--out", help="写入指定文件；不给则打到 stdout")
    parser.add_argument(
        "--archive",
        action="store_true",
        help="写入 <state_home>/diagnostics/<日期>.md 并按保留窗口清理旧报告；"
             "stdout 只留一行摘要（cron 用这个模式）",
    )
    parser.add_argument("--out-dir", help="配合 --archive；默认 <state_home>/diagnostics")
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出当日结构化诊断（机器可读），而不是 Markdown 报告",
    )
    parser.add_argument(
        "--rollup",
        action="store_true",
        help="读归档目录里最近 --days 天的结构化日报，输出"
             "新问题/重复问题/已验证修复的聚合摘要",
    )
    parser.add_argument("--days", type=int, default=5, help="配合 --rollup，默认 5 个自然日")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    state_home = os.path.expanduser(args.state_home)
    out_dir = os.path.expanduser(args.out_dir or os.path.join(state_home, "diagnostics"))

    if args.rollup:
        rollup = build_rollup(
            load_daily_reports(out_dir, args.days, args.date), days=args.days
        )
        print(json.dumps(rollup, ensure_ascii=False, indent=2))
        return 0

    if args.json:
        print(json.dumps(
            build_structured_report(
                args.date,
                state_home,
                os.path.expanduser(args.openclaw_db),
                os.path.expanduser(args.openclaw_log_dir),
            ),
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    collected = collect_all(
        args.date,
        state_home,
        os.path.expanduser(args.openclaw_db),
        os.path.expanduser(args.openclaw_log_dir),
    )
    report = build_report(
        args.date,
        state_home,
        os.path.expanduser(args.openclaw_db),
        os.path.expanduser(args.openclaw_log_dir),
        collected=collected,
    )

    if args.archive:
        print(_archive(args, report=report, collected=collected, out_dir=out_dir))
        return 0

    if args.out:
        target = os.path.expanduser(args.out)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(report)
        size_kb = os.path.getsize(target) / 1024
        print(f"已写入 {target}（{size_kb:.1f} KB）")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
