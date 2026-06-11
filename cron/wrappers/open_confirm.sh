#!/bin/bash
# 开盘确认 — 从当天竞价短名单生成前5确认标的
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PY="${HERMES_PYTHON:-$HERMES_HOME/hermes-agent/venv/bin/python3}"
SCRIPT="$HERMES_HOME/skills/daban-stock-picker/scripts/open_confirmation.py"

"$PY" "$SCRIPT" --limit 5 --json 2>&1
