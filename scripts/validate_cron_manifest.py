#!/usr/bin/env python3
"""Cron Manifest 校验器 — 严格模式"""

import json
import sys
import os
import re

# Direct cron/CLI execution puts only ``scripts/`` on sys.path.  Bootstrap the
# repository root before importing the shared package, just like the runtime
# entrypoints do.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import manifest_command  # noqa: E402

REQUIRED = ["id", "name", "schedule", "timezone", "cwd",
            "enabled", "external", "expected_output", "silent_when_no_signal",
            "execution_mode", "context_scope", "deliver", "max_output_chars",
            "context_from", "artifact_path_template", "allowed_state_writes",
            "run"]
VALID_OUTPUTS = {"json", "text", "none"}
VALID_EXECUTION_MODES = {"isolated_subprocess"}
VALID_CONTEXT_SCOPES = {"cron"}
VALID_DELIVER = {"origin", "local", "silent", "feishu_direct"}
VALID_TRADING_DAY_POLICIES = {"required", "calendar_day"}
VALID_DEPENDENCY_DATE_MODES = {
    "latest",
    "same_trading_date",
    "same_batch",
    "previous_trading_day",
}
ARTIFACT_TEMPLATE = "{cron_output_dir}/{job_id}/{run_id}.json"
DAG_RUNNER_SCRIPT = "scripts/run_agent_dag.py"
SINGLE_JOB_RUNNER_SCRIPT = "scripts/agent_job_runner.py"
PYTHON_HEADS = {"python", "python3"}
FORBIDDEN_TOP_LEVEL_SCRIPTS = {
    "capital_flow_monitor.py",
    "event_calendar.py",
    "four_dim_scorer.py",
    "hk_a_linkage.py",
    "institution_tracker.py",
    "intraday_monitor.py",
    "performance_tracker.py",
    "portfolio_manager.py",
    "recommendation_audit.py",
    "serenity_to_feishu.py",
}
PLACEHOLDER_RE = re.compile(r'\{(\w+)\}')

#: 超时分级约定。档位边界取自 2026-08 manifest 里 60 个作业的实际分布，
#: 那里有三个自然簇：≤60s（27 个）、90–300s（26 个）、420–2400s（7 个）。
#:
#: 档位与 timeout 值一一对应，本身不携带额外信息 —— 它是一个**校验和**，
#: 不是语义分类。价值在于：把超时调过一个数量级边界时，必须同时改档位，
#: diff 里因此一定看得见（issue #159 里"调大超时"被当成方案用了三次，
#: 每次都只改了一个数字）。语义分类（触网/纯计算/推送）刻意没做：它无法
#: 机械校验，标错比不标更糟。真正靠数据说话的那一半在
#: ``cron_budget_report.py`` 的 p95 余量守卫里。
TIMEOUT_TIERS = {
    "short": (10, 60),
    "standard": (61, 300),
    "long": (301, 2400),
}
#: 分钟级预算必须写明为什么 —— 唯一能拦住"再调大一点"的东西是一句要
#: 过 review 的理由。
TIMEOUT_RATIONALE_TIERS = {"long"}
#: hermes_job_runner.run_job 在 run.timeout_seconds 缺省时用的值。
DEFAULT_TIMEOUT_SECONDS = 120


def _timeout_tier_errors(run):
    """Errors for one job's declared timeout tier."""
    tier = run.get("timeout_tier")
    if tier is None:
        return [
            "run.timeout_tier is required; declare one of "
            + ", ".join(sorted(TIMEOUT_TIERS))
        ]
    if tier not in TIMEOUT_TIERS:
        return [f"invalid run.timeout_tier: {tier}"]
    errors = []
    timeout = run.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    low, high = TIMEOUT_TIERS[tier]
    if isinstance(timeout, int) and not isinstance(timeout, bool):
        if not low <= timeout <= high:
            errors.append(
                f"run.timeout_seconds {timeout} is outside tier "
                f"{tier} band [{low}, {high}]"
            )
    rationale = run.get("timeout_rationale")
    if tier in TIMEOUT_RATIONALE_TIERS and not str(rationale or "").strip():
        errors.append(
            f"run.timeout_tier {tier} requires a non-empty run.timeout_rationale"
        )
    return errors


def _is_dag_entry(argv):
    """argv form of `python scripts/run_agent_dag.py <job> [--emit-target]`."""
    if len(argv) not in (3, 4):
        return False
    if argv[0] not in PYTHON_HEADS or argv[1] != DAG_RUNNER_SCRIPT:
        return False
    if not re.fullmatch(r"[\w-]+", str(argv[2])):
        return False
    return len(argv) == 3 or argv[3] == "--emit-target"


def _calls_job_runner(argv):
    return any(
        str(item) in (DAG_RUNNER_SCRIPT, SINGLE_JOB_RUNNER_SCRIPT) for item in argv
    )


def _expand_cron_field(value, min_value, max_value):
    if value == "*":
        return set(range(min_value, max_value + 1))
    result = set()
    for part in value.split(","):
        if part.startswith("*/"):
            step = int(part[2:])
            result.update(range(min_value, max_value + 1, step))
        elif "-" in part:
            start, end = part.split("-", 1)
            result.update(range(int(start), int(end) + 1))
        else:
            result.add(int(part))
    return result


def _schedule_slots(schedule):
    minute, hour, _dom, _month, dow = schedule.split()
    minutes = _expand_cron_field(minute, 0, 59)
    hours = _expand_cron_field(hour, 0, 23)
    dows = _expand_cron_field(dow, 0, 7)
    return {(d if d != 7 else 0, h, m) for d in dows for h in hours for m in minutes}


def validate(filepath):
    if not os.path.exists(filepath):
        print(f"FAIL: {filepath} not found")
        return False

    with open(filepath) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"FAIL: JSON parse error: {e}")
            return False

    errors = []
    jobs = data.get("jobs", [])
    if not jobs:
        errors.append("no jobs defined")
    default_day_policy = data.get("default_trading_day_policy", "required")
    if default_day_policy not in VALID_TRADING_DAY_POLICIES:
        errors.append(
            f"invalid default_trading_day_policy: {default_day_policy}"
        )

    # 档位表在 manifest 里复述一份是给编辑超时的人看的；两处一旦漂移，
    # 读 manifest 的人会照着一个不生效的区间改。所以复述必须逐字相等。
    declared_bands = data.get("timeout_tier_bands")
    if declared_bands is not None:
        restated = {
            name: tuple(band)
            for name, band in declared_bands.items()
            if not name.startswith("_")
        }
        if restated != TIMEOUT_TIERS:
            errors.append(
                f"timeout_tier_bands must restate the validator bands: {TIMEOUT_TIERS}"
            )

    ids = set()
    logical_signatures = set()
    dependency_graph = {}
    schedule_slots = {}
    for i, job in enumerate(jobs):
        jid = job.get("id", f"#{i}")

        for field in REQUIRED:
            if field not in job:
                errors.append(f"job[{i}] ({jid}) missing required: {field}")

        if jid in ids:
            errors.append(f"job[{i}] duplicate id: {jid}")
        ids.add(jid)
        command_signature = tuple(job.get("command_argv") or [])
        signature = (
            str(job.get("name") or ""),
            str(job.get("schedule") or ""),
            command_signature,
        )
        if signature in logical_signatures:
            errors.append(
                f"job[{i}] ({jid}) duplicates logical job name/schedule/command"
            )
        logical_signatures.add(signature)
        dependency_mode = (job.get("dependency_policy") or {}).get(
            "trading_date",
            "same_trading_date",
        )
        dependency_graph[jid] = {
            "dependencies": list(job.get("context_from") or []),
            "mode": dependency_mode,
        }

        if job.get("schedule"):
            parts = job["schedule"].split()
            if len(parts) != 5:
                errors.append(f"job[{i}] ({jid}) invalid cron: {job['schedule']}")
            elif job.get("enabled", True):
                try:
                    for slot in _schedule_slots(job["schedule"]):
                        schedule_slots.setdefault(slot, []).append(jid)
                except ValueError as e:
                    errors.append(f"job[{i}] ({jid}) invalid cron field: {e}")

        if job.get("timezone") != "Asia/Shanghai":
            errors.append(f"job[{i}] ({jid}) timezone: {job.get('timezone', 'missing')}")

        day_policy = job.get("trading_day_policy", default_day_policy)
        if day_policy not in VALID_TRADING_DAY_POLICIES:
            errors.append(
                f"job[{i}] ({jid}) invalid trading_day_policy: {day_policy}"
            )

        if not isinstance(job.get("enabled"), bool):
            errors.append(f"job[{i}] ({jid}) enabled must be boolean")

        if not isinstance(job.get("external"), bool):
            errors.append(f"job[{i}] ({jid}) external must be boolean")

        output = job.get("expected_output")
        if output and output not in VALID_OUTPUTS:
            errors.append(f"job[{i}] ({jid}) invalid expected_output: {output}")

        silent = job.get("silent_when_no_signal")
        if silent is not None and not isinstance(silent, bool):
            errors.append(f"job[{i}] ({jid}) silent_when_no_signal must be boolean")

        adaptive_backoff = job.get("adaptive_backoff")
        if adaptive_backoff is not None and not isinstance(adaptive_backoff, bool):
            errors.append(f"job[{i}] ({jid}) adaptive_backoff must be boolean")

        if job.get("execution_mode") not in VALID_EXECUTION_MODES:
            errors.append(f"job[{i}] ({jid}) invalid execution_mode: {job.get('execution_mode')}")

        if job.get("context_scope") not in VALID_CONTEXT_SCOPES:
            errors.append(f"job[{i}] ({jid}) invalid context_scope: {job.get('context_scope')}")

        if job.get("deliver") not in VALID_DELIVER:
            errors.append(f"job[{i}] ({jid}) invalid deliver: {job.get('deliver')}")

        max_chars = job.get("max_output_chars")
        if not isinstance(max_chars, int) or not (1 <= max_chars <= 8000):
            errors.append(f"job[{i}] ({jid}) max_output_chars must be int in 1..8000")

        if not isinstance(job.get("context_from"), list):
            errors.append(f"job[{i}] ({jid}) context_from must be list")
        elif not all(isinstance(x, str) for x in job["context_from"]):
            errors.append(f"job[{i}] ({jid}) context_from entries must be strings")

        dependency_policy = job.get("dependency_policy", {})
        if not isinstance(dependency_policy, dict):
            errors.append(f"job[{i}] ({jid}) dependency_policy must be object")
        else:
            date_mode = dependency_policy.get("trading_date", "same_trading_date")
            if date_mode not in VALID_DEPENDENCY_DATE_MODES:
                errors.append(f"job[{i}] ({jid}) invalid dependency trading_date: {date_mode}")
            max_age = dependency_policy.get("max_age_minutes")
            if max_age is not None and (not isinstance(max_age, int) or max_age <= 0):
                errors.append(f"job[{i}] ({jid}) dependency max_age_minutes must be positive int")
            optional = dependency_policy.get("optional_jobs", [])
            if not isinstance(optional, list) or not all(isinstance(x, str) for x in optional):
                errors.append(f"job[{i}] ({jid}) dependency optional_jobs must be a string list")
            elif any(x not in job.get("context_from", []) for x in optional):
                errors.append(f"job[{i}] ({jid}) dependency optional_jobs must exist in context_from")

        if job.get("artifact_path_template") != ARTIFACT_TEMPLATE:
            errors.append(f"job[{i}] ({jid}) artifact_path_template must be {ARTIFACT_TEMPLATE}")

        if not isinstance(job.get("allowed_state_writes"), list):
            errors.append(f"job[{i}] ({jid}) allowed_state_writes must be list")
        elif any("~/.hermes" in x for x in job["allowed_state_writes"]):
            errors.append(
                f"job[{i}] ({jid}) allowed_state_writes must use $A_STOCK_STATE_HOME"
            )
        elif any("$HERMES_HOME" in x for x in job["allowed_state_writes"]):
            errors.append(
                f"job[{i}] ({jid}) business state writes must use $A_STOCK_STATE_HOME"
            )

        # 类型化命令边界：启用任务必须是 argv 数组，禁止 shell 字符串。
        # 字符串 command 只允许留在 disabled 任务上作为一个版本的迁移兼容，
        # 且永远不会被自动提升回 shell 执行路径。
        enabled_job = bool(job.get("enabled", True))
        outer_argv = job.get("command_argv")
        run = job.get("run")
        run_argv = (run or {}).get("argv") if isinstance(run, dict) else None

        if enabled_job:
            if "command" in job:
                errors.append(
                    f"job[{i}] ({jid}) enabled job must not use the shell string command; use command_argv"
                )
            if isinstance(run, dict) and "command" in run:
                errors.append(
                    f"job[{i}] ({jid}) enabled job must not use the shell string run.command; use run.argv"
                )
            errors.extend(
                f"job[{i}] ({jid}) {message}"
                for message in manifest_command.argv_errors(outer_argv, label="command_argv")
            )
        elif outer_argv is None and "command" not in job:
            errors.append(f"job[{i}] ({jid}) missing required: command_argv")
        elif outer_argv is not None:
            errors.extend(
                f"job[{i}] ({jid}) {message}"
                for message in manifest_command.argv_errors(outer_argv, label="command_argv")
            )

        outer_list = outer_argv if isinstance(outer_argv, list) else []
        if enabled_job and job.get("external") and not _is_dag_entry(outer_list):
            errors.append(
                f"job[{i}] ({jid}) command_argv must route through {DAG_RUNNER_SCRIPT}"
            )

        if not isinstance(run, dict):
            errors.append(f"job[{i}] ({jid}) run must be object")
            run_list = []
        else:
            if enabled_job or run_argv is not None:
                errors.extend(
                    f"job[{i}] ({jid}) {message}"
                    for message in manifest_command.argv_errors(run_argv, label="run.argv")
                )
            elif "command" not in run:
                errors.append(f"job[{i}] ({jid}) run must define argv")
            run_list = run_argv if isinstance(run_argv, list) else []
            if _calls_job_runner(run_list):
                errors.append(f"job[{i}] ({jid}) run.argv must not call a job runner recursively")
            for script in FORBIDDEN_TOP_LEVEL_SCRIPTS:
                if f"scripts/{script}" in run_list:
                    errors.append(
                        f"job[{i}] ({jid}) run.argv must use canonical skills/... path, not scripts/{script}"
                    )
            timeout = run.get("timeout_seconds")
            if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
                errors.append(f"job[{i}] ({jid}) run.timeout_seconds must be positive int")
            errors.extend(
                f"job[{i}] ({jid}) {message}"
                for message in _timeout_tier_errors(run)
            )
            # run.env carries per-job business flags with the manifest. It must
            # not reach the keys that decide state home, run identity or PATH —
            # the runner ignores those at runtime, this fails the same input
            # loudly at gate time.
            errors.extend(
                f"job[{i}] ({jid}) {message}"
                for message in manifest_command.env_errors(run)
            )

        # 仓库 cron 必须自包含。不能依赖 Gateway/agent 在触发时动态注入模板变量，
        # 否则会重新走 in-process AIAgent import 路径，触发上下文污染和导入冲突。
        placeholders = manifest_command.undeclared_placeholders(
            [*outer_list, *run_list]
        )
        if placeholders:
            errors.append(
                f"job[{i}] ({jid}) must be self-contained; placeholders are not allowed: {placeholders}"
            )
        if "template_vars" in job:
            errors.append(f"job[{i}] ({jid}) template_vars are not allowed in isolated cron manifest")

    for slot, slot_jobs in schedule_slots.items():
        if len(slot_jobs) > 2:
            dow, hour, minute = slot
            errors.append(
                f"cron slot overload dow={dow} {hour:02d}:{minute:02d}: {', '.join(slot_jobs)}"
            )

    for jid, node in dependency_graph.items():
        for dependency in node["dependencies"]:
            if dependency not in ids:
                errors.append(f"job ({jid}) references unknown dependency: {dependency}")

    visiting = set()
    visited = set()

    def _visit(jid, path):
        if jid in visiting:
            cycle = " -> ".join(path + [jid])
            errors.append(f"dependency cycle: {cycle}")
            return
        if jid in visited or jid not in dependency_graph:
            return
        visiting.add(jid)
        node = dependency_graph[jid]
        # A previous-trading-day edge deliberately links two batches. It cannot
        # form an execution cycle inside the current batch.
        dependencies = [] if node["mode"] == "previous_trading_day" else node["dependencies"]
        for dependency in dependencies:
            _visit(dependency, path + [jid])
        visiting.remove(jid)
        visited.add(jid)

    for jid in dependency_graph:
        _visit(jid, [])

    if errors:
        for e in errors:
            print(f"  FAIL: {e}")
        return False

    external_count = sum(1 for j in jobs if j.get("external"))
    local_count = len(jobs) - external_count
    print(f"OK: {len(jobs)} jobs ({local_count} local, {external_count} external)")
    return True


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "cron/hermes-cron-manifest.json"
    ok = validate(path)
    sys.exit(0 if ok else 1)
