#!/bin/bash
# 集合竞价收口 — 从动态观察池生成前20短名单
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PY="${HERMES_PYTHON:-$HERMES_HOME/hermes-agent/venv/bin/python3}"
SCRIPT="$HERMES_HOME/skills/daban-stock-picker/scripts/auction_collector.py"

"$PY" "$SCRIPT" --finalize --shortlist-limit 20 --json 2>&1
