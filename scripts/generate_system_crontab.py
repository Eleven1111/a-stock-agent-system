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
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "common"))

from cron_roles import is_scheduled  # noqa: E402


def load_manifest(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def crontab_lines(
    manifest: Dict[str, Any],
    repo_dir: str,
    hermes_home: str,
    python: str,
    state_home: str | None = None,
) -> List[str]:
    lines = [
        "# A-stock isolated cron fallback. Generated from cron/hermes-cron-manifest.json.",
        f"HERMES_HOME={shlex.quote(hermes_home)}",
        f"A_STOCK_STATE_HOME={shlex.quote(state_home or hermes_home)}",
    ]
    log_path = "$HERMES_HOME/cron/system-cron.log"
    repo = shlex.quote(repo_dir)
    py = shlex.quote(python)
    for job in manifest.get("jobs", []):
        if not is_scheduled(job):
            continue
        if "{" in job.get("command", "") or "}" in job.get("command", ""):
            raise ValueError(f"job {job['id']} is not self-contained: {job['command']}")
        schedule = job["schedule"]
        command = (
            f"cd {repo} && A_STOCK_RUNTIME=hermes {py} scripts/run_agent_dag.py "
            f"{shlex.quote(job['id'])} --emit-target >> {log_path} 2>&1"
        )
        lines.append(f"{schedule} {command}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate system crontab lines for isolated A-stock jobs")
    parser.add_argument("--manifest", default="cron/hermes-cron-manifest.json")
    parser.add_argument("--repo-dir", default=os.getcwd())
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    parser.add_argument("--state-home", default=os.environ.get("A_STOCK_STATE_HOME"))
    parser.add_argument("--python", default=os.environ.get("HERMES_CRON_PYTHON") or "python")
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    print("\n".join(crontab_lines(
        manifest,
        os.path.abspath(args.repo_dir),
        os.path.abspath(os.path.expanduser(args.hermes_home)),
        args.python,
        (
            os.path.abspath(os.path.expanduser(args.state_home))
            if args.state_home
            else None
        ),
    )))


if __name__ == "__main__":
    main()
