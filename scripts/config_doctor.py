#!/usr/bin/env python3
"""Validate every registered repository configuration file."""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "skills", "common"))

from config_registry import validate_registered_configs  # noqa: E402


def main() -> int:
    report = validate_registered_configs()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
