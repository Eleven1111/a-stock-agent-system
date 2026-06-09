#!/bin/bash
# 盘中异动监控（脚本模式，no_agent=True）
# 每10分钟检测跟踪标的涨跌停/放量/急涨急跌
# 非交易时段自动静默，有信号才输出
#
# Usage: bash intraday_monitor.sh
# Cron: */10 9-15 * * 1-5

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PY="$HERMES_HOME/hermes-agent/venv/bin/python3"
SCRIPT="$HERMES_HOME/skills/stock-triage/scripts/intraday_monitor.py"

# 加载 .env
if [ -f "$HERMES_HOME/.env" ]; then
  set -a; source <(grep -v '^#' "$HERMES_HOME/.env" | grep -v '^$'); set +a
fi

"$PY" "$SCRIPT" 2>&1
