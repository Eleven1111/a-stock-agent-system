#!/bin/bash
# 收盘四维打分（脚本模式，no_agent=True）
# 复核动态观察池前20只
#
# Usage: bash four_dim_scorer.sh
# Cron: 18 15 * * 1-5

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PY="${HERMES_PYTHON:-$HERMES_HOME/hermes-agent/venv/bin/python3}"
SCRIPT="$HERMES_HOME/skills/stock-triage/scripts/batch_four_dim_scorer.py"

# 加载 .env（NO_PROXY 等，用于 datacenter/push2 API）
if [ -f "$HERMES_HOME/.env" ]; then
  set -a; source <(grep -v '^#' "$HERMES_HOME/.env" | grep -v '^$'); set +a
fi

"$PY" "$SCRIPT" --limit 20 --json 2>&1
