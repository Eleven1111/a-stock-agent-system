#!/usr/bin/env python3
"""Cron Manifest 校验器"""

import json, sys, os

REQUIRED = ["id", "name", "schedule", "timezone", "enabled", "external"]

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
        for field in REQUIRED:
            if field not in job:
                errors.append(f"job[{i}] missing required field: {field}")

        jid = job.get("id", f"#{i}")
        if jid in ids:
            errors.append(f"job[{i}] duplicate id: {jid}")
        ids.add(jid)

        if job.get("schedule"):
            parts = job["schedule"].split()
            if len(parts) != 5:
                errors.append(f"job[{i}] ({jid}) invalid cron schedule: {job['schedule']}")

        if job.get("timezone") != "Asia/Shanghai":
            errors.append(f"job[{i}] ({jid}) timezone not Asia/Shanghai: {job['timezone']}")

        if not isinstance(job.get("enabled"), bool):
            errors.append(f"job[{i}] ({jid}) enabled must be boolean")

        if not isinstance(job.get("external"), bool):
            errors.append(f"job[{i}] ({jid}) external must be boolean")

    if errors:
        for e in errors:
            print(f"  FAIL: {e}")
        return False

    external_count = sum(1 for j in jobs if j.get("external"))
    print(f"OK: {len(jobs)} jobs, {external_count} external, {len(jobs) - external_count} local")
    return True

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "cron/hermes-cron-manifest.json"
    ok = validate(path)
    sys.exit(0 if ok else 1)
