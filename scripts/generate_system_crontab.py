#!/usr/bin/env python3
"""
Generate a Hermes system crontab fallback from the runtime-neutral manifest.

Use this when Hermes Gateway's in-process cron scheduler is unhealthy. The
generated lines run the shared agent job runner directly and therefore bypass
Gateway AIAgent imports while keeping the same per-run artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
from typing import Any, Dict, List


def load_manifest(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def crontab_lines(manifest: Dict[str, Any], repo_dir: str, hermes_home: str, python: str) -> List[str]:
    lines = [
        "# A-stock isolated cron fallback. Generated from cron/hermes-cron-manifest.json.",
        f"HERMES_HOME={shlex.quote(hermes_home)}",
    ]
    log_path = "$HERMES_HOME/cron/system-cron.log"
    repo = shlex.quote(repo_dir)
    py = shlex.quote(python)
    for job in manifest.get("jobs", []):
        if not job.get("enabled", True):
            continue
        if "{" in job.get("command", "") or "}" in job.get("command", ""):
            raise ValueError(f"job {job['id']} is not self-contained: {job['command']}")
        schedule = job["schedule"]
        command = (
            f"cd {repo} && {py} scripts/agent_job_runner.py "
            f"{shlex.quote(job['id'])} --runtime hermes >> {log_path} 2>&1"
        )
        lines.append(f"{schedule} {command}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate system crontab lines for isolated A-stock jobs")
    parser.add_argument("--manifest", default="cron/hermes-cron-manifest.json")
    parser.add_argument("--repo-dir", default=os.getcwd())
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    parser.add_argument("--python", default=os.environ.get("HERMES_CRON_PYTHON") or "python")
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    print("\n".join(crontab_lines(
        manifest,
        os.path.abspath(args.repo_dir),
        os.path.abspath(os.path.expanduser(args.hermes_home)),
        args.python,
    )))


if __name__ == "__main__":
    main()
