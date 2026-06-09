#!/usr/bin/env python3
"""
Diagnose Hermes Gateway cron import/schedule failures.

This script is intended to run on the deployment machine where Hermes is
installed. It does not import Hermes modules directly; it probes import
resolution in subprocesses to avoid reproducing partial imports in this process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from typing import Any, Dict, Optional


def default_hermes_home() -> str:
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def default_agent_dir(hermes_home: str) -> str:
    return os.environ.get("HERMES_AGENT_DIR") or os.path.join(hermes_home, "hermes-agent")


def default_python(agent_dir: str) -> str:
    if os.environ.get("HERMES_PYTHON"):
        return os.environ["HERMES_PYTHON"]
    candidate = os.path.join(agent_dir, "venv", "bin", "python3")
    return candidate if os.path.exists(candidate) else sys.executable


def file_fingerprint(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    line_count = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
            line_count += chunk.count(b"\n")
    return {"path": path, "lines": line_count, "sha256": h.hexdigest()}


def probe_run_agent(python_exe: str, cwd: str) -> Dict[str, Any]:
    code = r"""
import importlib.util, json
spec = importlib.util.find_spec("run_agent")
origin = spec.origin if spec else None
has_aiagent = None
error = None
if origin:
    try:
        text = open(origin, "r", encoding="utf-8", errors="ignore").read()
        has_aiagent = "class AIAgent" in text or "AIAgent =" in text
    except Exception as exc:
        error = str(exc)
print(json.dumps({"origin": origin, "has_aiagent_text": has_aiagent, "error": error}))
"""
    try:
        result = subprocess.run(
            [python_exe, "-c", code],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return {"cwd": cwd, "error": str(exc)}
    try:
        payload = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        payload = {"stdout": result.stdout}
    payload.update({"cwd": cwd, "returncode": result.returncode, "stderr": result.stderr})
    return payload


def diagnose(hermes_home: str, agent_dir: str, python_exe: str) -> Dict[str, Any]:
    source_run_agent = os.path.join(agent_dir, "run_agent.py")
    neutral_cwd = hermes_home if os.path.isdir(hermes_home) else os.path.expanduser("~")
    source_probe = probe_run_agent(python_exe, agent_dir) if os.path.isdir(agent_dir) else {"error": "agent_dir missing"}
    neutral_probe = probe_run_agent(python_exe, neutral_cwd)

    source_fp = file_fingerprint(source_run_agent)
    neutral_fp = file_fingerprint(neutral_probe.get("origin")) if neutral_probe.get("origin") else None
    shadowing = bool(
        source_fp
        and source_probe.get("origin")
        and os.path.abspath(source_probe["origin"]) == os.path.abspath(source_run_agent)
        and neutral_probe.get("origin")
        and os.path.abspath(neutral_probe["origin"]) != os.path.abspath(source_run_agent)
    )
    mismatch = bool(source_fp and neutral_fp and source_fp["sha256"] != neutral_fp["sha256"])

    state_db = os.path.join(hermes_home, "state.db")
    cron_output = os.path.join(hermes_home, "cron", "output")
    recommendations = []
    if shadowing:
        recommendations.append(
            "Gateway is resolving run_agent.py from the source cwd. Start gateway from $HERMES_HOME or another neutral cwd."
        )
    if mismatch:
        recommendations.append(
            "Source run_agent.py and installed run_agent.py differ. Use one import source only; prefer neutral cwd + installed package."
        )
    if os.path.exists(state_db):
        recommendations.append(
            "If schedule recursion/state-loss persists, export needed jobs, stop Gateway, clear/recreate affected cron jobs from the manifest."
        )
    if not recommendations:
        recommendations.append("No run_agent shadowing detected by this probe.")

    return {
        "schema": "hermes_gateway_doctor_v1",
        "hermes_home": hermes_home,
        "agent_dir": agent_dir,
        "python": python_exe,
        "source_run_agent": source_fp,
        "probe_from_agent_cwd": source_probe,
        "probe_from_neutral_cwd": neutral_probe,
        "shadowing_detected": shadowing,
        "source_install_mismatch": mismatch,
        "state_db": {"path": state_db, "exists": os.path.exists(state_db)},
        "cron_output": {"path": cron_output, "exists": os.path.exists(cron_output)},
        "recommendations": recommendations,
    }


def write_safe_launcher(hermes_home: str, agent_dir: str, python_exe: str) -> str:
    path = os.path.join(hermes_home, "run_gateway_safe.sh")
    content = f"""#!/usr/bin/env bash
set -euo pipefail
export HERMES_HOME="${{HERMES_HOME:-{hermes_home}}}"
cd "$HERMES_HOME"
exec "{python_exe}" -m hermes_cli.main gateway run "$@"
"""
    os.makedirs(hermes_home, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose Hermes Gateway import/schedule hazards")
    parser.add_argument("--hermes-home", default=default_hermes_home())
    parser.add_argument("--agent-dir")
    parser.add_argument("--python")
    parser.add_argument("--write-launcher", action="store_true")
    args = parser.parse_args()

    hermes_home = os.path.abspath(os.path.expanduser(args.hermes_home))
    agent_dir = os.path.abspath(os.path.expanduser(args.agent_dir or default_agent_dir(hermes_home)))
    python_exe = os.path.abspath(os.path.expanduser(args.python or default_python(agent_dir)))
    result = diagnose(hermes_home, agent_dir, python_exe)
    if args.write_launcher:
        result["safe_launcher"] = write_safe_launcher(hermes_home, agent_dir, python_exe)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
