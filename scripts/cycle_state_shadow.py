#!/usr/bin/env python3
"""
周期状态影子 emitter（打板优化方案 P1-4）— SHADOW ONLY
======================================================
每日读取实盘选股产物 hot_money_selection_latest.json 里已经算好的 market_state
（S0-S6，classify_market_state 产出），在其上推进周期记忆（日龄 + 分歧计数），
并取沪指日线算指数趋势闸门，把两者合成一行影子记录 append 到 cycle_shadow_log.jsonl。

升级方案 P0-a/P0-d 起，同一次运行还负责两件事：
- 用当日已固化的 discovery 输入快照产出 ``sentiment_daily`` 记录（只读，不取数）；
- 落一份**双轨对照**块 {五档温度, S0-S6, S_t 分档, ΔS}，供 P1 校准三套口径。

它**只写影子文件与研究数据集**：不改任何实盘信号、排序、评分或仓位。P2 才用这份
日志做"假想拦截 vs 实际结算"对照，决定哪些闸门值得真启用。

Fail-closed：选股产物缺失/过期 → market_state 视为不可用，仍落一行注明缺口的
影子记录（保留分歧计数结转），不静默跳过；情绪数据集输入缺失 → 该块记
``blocked`` 并说明缺口，S_t 记 ``unavailable``，绝不用旧数据顶替。
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
import sentiment_daily  # noqa: E402
import sentiment_score  # noqa: E402
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


def build_sentiment_track(
    selection_state: Mapping[str, Any], asof: str, *, produce: bool = True
) -> dict[str, Any]:
    """P0-d 双轨对照：五档温度 / S0-S6 / S_t 分档 / ΔS 同日并排落盘。

    先产出当日 sentiment_daily 记录（``produce=False`` 时只读已有序列），再在完整
    序列上算 S_t。三套口径互不覆盖：S_t 不可用时另两套照记，缺口写进 reason。
    """
    market_timing = dict(selection_state.get("market_timing") or {})
    market_state = dict(selection_state.get("market_state") or {})
    dataset = (
        sentiment_daily.produce_daily_record(asof, selection_state)
        if produce
        else {"status": "skipped"}
    )
    series = sentiment_daily.load_summary()
    score = sentiment_score.compute_sentiment_score(series)
    return {
        "schema": "sentiment_dual_track_v1",
        "asof": asof,
        "calibrated": False,
        "shadow_only": True,
        "temperature_tier": (market_timing.get("temperature") or {}).get("tier"),
        "market_state": market_state.get("dominant_state"),
        "market_state_label": market_state.get("dominant_label"),
        "sentiment_score": score.get("score"),
        "sentiment_band": score.get("band"),
        "sentiment_delta": score.get("delta"),
        "sentiment_delta_squared": score.get("delta_squared"),
        "sentiment_status": score.get("status"),
        "sentiment_reason": score.get("reason"),
        # LeaderConfirm 是 P2 的 LeaderScore 产物，本阶段系统里没有这个字段：
        # 传 None 让谓词按"证据不可用"判否（缺证据 ≠ 满足），而不是就地编一个代理。
        "ice_confirm": sentiment_score.ice_point_confirmed(
            score,
            leader_confirm=None,
            sector_breadth_top=sentiment_daily.sector_breadth_top(selection_state),
        ),
        "dataset": dataset,
        "series_days": len(series),
    }


def run(*, skip_index: bool = False, skip_dataset: bool = False) -> dict[str, Any]:
    selection_state = _selection_state()
    asof = _asof(selection_state)
    market_state = selection_state.get("market_state")
    index_trend: Optional[dict[str, Any]] = None
    if not skip_index:
        index_trend = index_trend_gate.fetch_index_trend()
    return market_cycle_state.record_cycle_state(
        market_state,
        asof=asof,
        index_trend=index_trend,
        sentiment_track=build_sentiment_track(
            selection_state, asof, produce=not skip_dataset
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="周期状态影子 emitter（P1）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--skip-index", action="store_true",
                        help="跳过指数取数（离线/测试用）")
    parser.add_argument("--skip-dataset", action="store_true",
                        help="不产出当日 sentiment_daily 记录（只读已有序列）")
    args = parser.parse_args()
    row = run(skip_index=args.skip_index, skip_dataset=args.skip_dataset)
    if args.json:
        print(json.dumps(row, ensure_ascii=False, indent=2))
    else:
        mem = row.get("cycle_memory") or {}
        cons = row.get("cycle_shadow_constraints") or {}
        track = row.get("sentiment_track") or {}
        print(f"[{row.get('asof')}] 周期={mem.get('cycle_phase') or '不可用'} "
              f"分歧计数={mem.get('divergence_count')} "
              f"影子拦新={cons.get('would_block_new_positions')} "
              f"降级弱转强={cons.get('would_downgrade_second_board_w2s')}")
        print(f"  双轨: 五档={track.get('temperature_tier') or '不可用'} "
              f"状态={track.get('market_state') or '不可用'} "
              f"S_t={track.get('sentiment_score') if track.get('sentiment_score') is not None else '不可用'}"
              f"({track.get('sentiment_band') or track.get('sentiment_reason')}) "
              f"ΔS={track.get('sentiment_delta')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
