#!/bin/bash
# 盘前持仓综合检查（脚本模式，no_agent=True）
# 合并：分红除权事件扫描 + 持仓风控 → 单一输出
# 替代原"事件日历提醒"（周一 08:00）+"持仓风控检查"（盘后 15:10）
# 每日 08:30 运行
#
# Usage: bash portfolio_check.sh
# Cron: 30 8 * * 1-5

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PY="$HERMES_HOME/hermes-agent/venv/bin/python3"

# 加载 .env（NO_PROXY 等）
if [ -f "$HERMES_HOME/.env" ]; then
  set -a; source <(grep -v '^#' "$HERMES_HOME/.env" | grep -v '^$'); set +a
fi

echo "📊 **盘前综合检查**"
echo "⏰ $(date '+%Y-%m-%d %H:%M')"
echo ""

# 1. 事件扫描（分红除权/政策窗口）
echo "## 📅 事件扫描"
"$PY" "$HERMES_HOME/skills/stock-triage/scripts/event_calendar.py" --portfolio 2>&1 || echo "⚠️ 事件扫描异常"

echo ""

# 2. 持仓风控
echo "## 🛡️ 持仓风控"
"$PY" "$HERMES_HOME/skills/stock-triage/scripts/portfolio_manager.py" --check 2>&1 || echo "⚠️ 持仓检查异常"
