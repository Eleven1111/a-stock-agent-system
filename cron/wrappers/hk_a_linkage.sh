#!/bin/bash
# 港A联动监控（脚本模式，no_agent=True）
# 监控AH溢价 / 港股异动 / 南北向资金
#
# Usage: bash hk_a_linkage.sh
# Cron: 45 9,13,14 * * 1-5

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PY="$HERMES_HOME/hermes-agent/venv/bin/python3"
SCRIPT="$HERMES_HOME/skills/stock-triage/scripts/hk_a_linkage.py"

if [ -f "$HERMES_HOME/.env" ]; then
  set -a; source <(grep -v '^#' "$HERMES_HOME/.env" | grep -v '^$'); set +a
fi

"$PY" "$SCRIPT" 2>&1
