#!/usr/bin/env python3
"""Cron Manifest 校验器 — 严格模式"""

import json, sys, os, re

REQUIRED = ["id", "name", "schedule", "timezone", "command", "cwd",
            "enabled", "external", "expected_output", "silent_when_no_signal"]
VALID_OUTPUTS = {"json", "text", "none"}
PLACEHOLDER_RE = re.compile(r'\{(\w+)\}')


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

        # 占位符检测：command 中有占位符但没有 template_vars 声明 → 错误
        cmd = job.get("command", "")
        placeholders = PLACEHOLDER_RE.findall(cmd)
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
