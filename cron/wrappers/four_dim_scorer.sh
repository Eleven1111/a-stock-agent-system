#!/bin/bash
# 收盘四维打分（脚本模式，no_agent=True）
# 批量评分跟踪标的，替代原 agent 模式 cron
#
# Usage: bash four_dim_scorer.sh
# Cron: 8 15 * * 1-5

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PY="$HERMES_HOME/hermes-agent/venv/bin/python3"
SCRIPT="$HERMES_HOME/skills/stock-triage/scripts/batch_four_dim_scorer.py"

# 加载 .env（NO_PROXY 等，用于 datacenter/push2 API）
if [ -f "$HERMES_HOME/.env" ]; then
  set -a; source <(grep -v '^#' "$HERMES_HOME/.env" | grep -v '^$'); set +a
fi

# 跟踪标的（与 cron prompt 保持一致）
# 封测/AI + 电力/电网 + 煤炭
TARGETS="600011:华能国际,002156:通富微电,600584:长电科技,002185:华天科技,000021:深科技,600667:太极实业,600900:长江电力,600025:华能水电,000400:许继电气,600406:国电南瑞,000983:山西焦煤,601666:平煤股份,601001:晋控煤业"

"$PY" "$SCRIPT" --targets "$TARGETS" 2>&1
