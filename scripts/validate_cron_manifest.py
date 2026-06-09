#!/usr/bin/env python3
"""Cron Manifest 校验器 — 严格模式"""

import json
import sys
import os
import re

REQUIRED = ["id", "name", "schedule", "timezone", "command", "cwd",
            "enabled", "external", "expected_output", "silent_when_no_signal",
            "execution_mode", "context_scope", "deliver", "max_output_chars",
            "context_from", "artifact_path_template", "allowed_state_writes",
            "run"]
VALID_OUTPUTS = {"json", "text", "none"}
VALID_EXECUTION_MODES = {"isolated_subprocess"}
VALID_CONTEXT_SCOPES = {"cron"}
VALID_DELIVER = {"origin", "local", "silent"}
ARTIFACT_TEMPLATE = "{cron_output_dir}/{job_id}/{run_id}.json"
RUNNER_RE = re.compile(r"^python3?\s+scripts/hermes_job_runner\.py\s+[\w-]+")
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

    ids = set()
    schedule_slots = {}
    for i, job in enumerate(jobs):
        jid = job.get("id", f"#{i}")

        for field in REQUIRED:
            if field not in job:
                errors.append(f"job[{i}] ({jid}) missing required: {field}")

        if jid in ids:
            errors.append(f"job[{i}] duplicate id: {jid}")
        ids.add(jid)

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

        if job.get("artifact_path_template") != ARTIFACT_TEMPLATE:
            errors.append(f"job[{i}] ({jid}) artifact_path_template must be {ARTIFACT_TEMPLATE}")

        if not isinstance(job.get("allowed_state_writes"), list):
            errors.append(f"job[{i}] ({jid}) allowed_state_writes must be list")
        elif any("~/.hermes" in x for x in job["allowed_state_writes"]):
            errors.append(f"job[{i}] ({jid}) allowed_state_writes must use $HERMES_HOME, not ~/.hermes")

        cmd = job.get("command", "")
        if job.get("enabled", True) and job.get("external") and not RUNNER_RE.match(cmd):
            errors.append(f"job[{i}] ({jid}) command must route through scripts/hermes_job_runner.py")

        run = job.get("run")
        if not isinstance(run, dict):
            errors.append(f"job[{i}] ({jid}) run must be object")
            run_cmd = ""
        else:
            run_cmd = run.get("command", "")
            if not isinstance(run_cmd, str) or not run_cmd.strip():
                errors.append(f"job[{i}] ({jid}) run.command must be non-empty string")
            if RUNNER_RE.match(run_cmd):
                errors.append(f"job[{i}] ({jid}) run.command must not call hermes_job_runner recursively")
            for script in FORBIDDEN_TOP_LEVEL_SCRIPTS:
                if re.search(rf"python3?\s+scripts/{re.escape(script)}(\s|$)", run_cmd):
                    errors.append(
                        f"job[{i}] ({jid}) run.command must use canonical skills/... path, not scripts/{script}"
                    )
            timeout = run.get("timeout_seconds")
            if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
                errors.append(f"job[{i}] ({jid}) run.timeout_seconds must be positive int")

        # 占位符检测：command 中有占位符但没有 template_vars 声明 → 错误
        placeholders = PLACEHOLDER_RE.findall(cmd) + PLACEHOLDER_RE.findall(run_cmd)
        template_vars = job.get("template_vars")

        if placeholders:
            if not template_vars:
                errors.append(
                    f"job[{i}] ({jid}) command has placeholders {placeholders} "
                    f"but no template_vars defined"
                )
            else:
                # 严格校验：占位符集合必须是 template_vars 的子集
                placeholder_set = set(placeholders)
                vars_set = set(template_vars)
                missing = placeholder_set - vars_set
                if missing:
                    errors.append(
                        f"job[{i}] ({jid}) placeholders {sorted(missing)} "
                        f"not covered by template_vars={template_vars}"
                    )
                # 警告未使用的 template var（不阻断，仅输出）
                unused = vars_set - placeholder_set
                if unused:
                    print(f"  WARN: job[{i}] ({jid}) unused template_vars={sorted(unused)}")

    for slot, slot_jobs in schedule_slots.items():
        if len(slot_jobs) > 2:
            dow, hour, minute = slot
            errors.append(
                f"cron slot overload dow={dow} {hour:02d}:{minute:02d}: {', '.join(slot_jobs)}"
            )

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
