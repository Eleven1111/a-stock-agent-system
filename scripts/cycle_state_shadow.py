#!/usr/bin/env python3
"""
周期状态影子 emitter（打板优化方案 P1-4）— SHADOW ONLY
======================================================
每日读取实盘选股产物 hot_money_selection_latest.json 里已经算好的 market_state
（S0-S6，classify_market_state 产出），在其上推进周期记忆（日龄 + 分歧计数），
并取沪指日线算指数趋势闸门，把两者合成一行影子记录 append 到 cycle_shadow_log.jsonl。

它**只写影子文件**：不改任何实盘信号、排序、评分或仓位。P2 才用这份日志做
"假想拦截 vs 实际结算"对照，决定哪些闸门值得真启用。

Fail-closed：选股产物缺失/过期 → market_state 视为不可用，仍落一行注明缺口的
影子记录（保留分歧计数结转），不静默跳过。
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from typing import Any, Mapping, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(ROOT)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import index_trend_gate  # noqa: E402
import market_cycle_state  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import read_json  # noqa: E402


def _selection_state() -> dict[str, Any]:
    record = read_json(
        data_file("stock-triage", "hot_money_selection_latest.json"), {}
    )
    return record if isinstance(record, dict) else {}


def _asof(selection_state: Mapping[str, Any]) -> str:
    market_timing = selection_state.get("market_timing") or {}
    return str(
        market_timing.get("event_asof")
        or selection_state.get("asof")
        or date.today().isoformat()
    )


def run(*, skip_index: bool = False) -> dict[str, Any]:
    selection_state = _selection_state()
    asof = _asof(selection_state)
    market_state = selection_state.get("market_state")
    index_trend: Optional[dict[str, Any]] = None
    if not skip_index:
        index_trend = index_trend_gate.fetch_index_trend()
    return market_cycle_state.record_cycle_state(
        market_state, asof=asof, index_trend=index_trend
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="周期状态影子 emitter（P1）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--skip-index", action="store_true",
                        help="跳过指数取数（离线/测试用）")
    args = parser.parse_args()
    row = run(skip_index=args.skip_index)
    if args.json:
        print(json.dumps(row, ensure_ascii=False, indent=2))
    else:
        mem = row.get("cycle_memory") or {}
        cons = row.get("cycle_shadow_constraints") or {}
        print(f"[{row.get('asof')}] 周期={mem.get('cycle_phase') or '不可用'} "
              f"分歧计数={mem.get('divergence_count')} "
              f"影子拦新={cons.get('would_block_new_positions')} "
              f"降级弱转强={cons.get('would_downgrade_second_board_w2s')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
