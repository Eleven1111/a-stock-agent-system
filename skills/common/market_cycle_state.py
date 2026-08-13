#!/usr/bin/env python3
"""
市场情绪周期记忆层（market_cycle_state）— 打板优化方案 P1，SHADOW ONLY
======================================================================
`market_temperature.classify_market_state` 已经把五档温度 + 广度/拥挤/轮动证据
映射成 S0-S6 的市场状态机（含 previous_state 滞后切换）。它是**无记忆的当日快照**：
只知道"今天是 S5"，不知道"这是第几次进 S5"、"退潮到第几天了"。

本模块只加一层记忆，不重造状态机：
- days_in_state  —— 周期日龄（退潮 D1/D2 之分，手册 L-03）
- divergence_count —— 自上次主升确认以来进入分歧(S5)的次数（首次分歧 vs 二次
  以上分歧，手册 E-02："首次分歧次日大概率转一致，二次以上防退潮"）

并把记忆映射成**影子约束**（would_block / would_downgrade）：只描述"若启用会拦
什么"，绝不真的拦——启用决策留给 P2（用影子日志做假想拦截 vs 实际结算对照）。

Fail-closed：market_state 不可用（温度缺失/过期）时 available=False，日龄置 None，
不推进日龄，但保留上一日的分歧计数（数据缺口不该把分歧计数清零）。

阈值走 config/daban_thresholds.yaml 的 emotion_cycle 节（daban_config 统一加载，
缺块回退 DEFAULTS，行为与未配置时一致）。纯标准库，cron-safe，不触网。
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Any, Mapping, Optional

from daban_config import section as _daban_section
from paths import data_file
from state_store import atomic_write_json, file_lock, read_json

SCHEMA = "market_cycle_memory_v1"

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "second_divergence_min": 2,
    "weaken_states": ["S6"],
    "divergence_states": ["S5"],
    "rise_reset_states": ["S3"],
}

# 影子记录落盘位置（与 signal_context / theme_registry 同在 stock-triage data）。
_LATEST_FILE = ("stock-triage", "market_cycle_memory.json")
_SHADOW_LOG = ("stock-triage", "cycle_shadow_log.jsonl")


def _config(config: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    # daban_config.section 自身 fail-safe（缺块/损坏回退 DEFAULTS），从不抛出。
    base = dict(_DEFAULTS)
    if config is None:
        config = _daban_section("emotion_cycle")
    for key, value in dict(config or {}).items():
        base[key] = value
    return base


def _unknown_memory(prev: Mapping[str, Any] | None, asof: str) -> dict[str, Any]:
    """market_state 不可用时的 fail-closed 记忆：不臆造方向，日龄置 None，
    但把分歧计数从上一日结转（数据缺口 != 分歧归零）。"""
    prev = prev or {}
    return {
        "schema": SCHEMA,
        "asof": asof,
        "available": False,
        "dominant_state": None,
        "state_label": None,
        "previous_state": prev.get("dominant_state"),
        "days_in_state": None,
        "divergence_count": int(prev.get("divergence_count") or 0),
        "is_first_divergence": False,
        "cycle_phase": None,
        "notes": ["market_state 不可用，周期记忆不推进日龄（fail-closed）"],
    }


def advance_cycle_memory(
    prev_memory: Mapping[str, Any] | None,
    market_state: Mapping[str, Any] | None,
    *,
    asof: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """在 S0-S6 状态机之上推进记忆：日龄 + 分歧计数。纯函数，可单测。

    prev_memory 为"上一交易日"的记忆记录；同日重复推进（prev.asof == asof）
    幂等返回 prev，避免把分歧计数重复 +1。
    """
    cfg = _config(config)
    prev = dict(prev_memory or {})

    if prev.get("asof") == asof and prev.get("schema") == SCHEMA:
        return prev  # 幂等：同一交易日重复计算不重复计数

    ms = dict(market_state or {})
    state = ms.get("dominant_state")
    if not ms.get("available") or not state:
        return _unknown_memory(prev, asof)

    prev_state = prev.get("dominant_state")
    prev_count = int(prev.get("divergence_count") or 0)
    divergence_states = {str(s) for s in cfg.get("divergence_states") or []}
    rise_reset = {str(s) for s in cfg.get("rise_reset_states") or []}

    in_divergence = state in divergence_states
    if state in rise_reset:
        divergence_count = 0  # 分歧转一致/主升确认，重新起算
    elif in_divergence and not prev_memory:
        divergence_count = 1  # 冷启动第一条就在分歧态：当前即处于第一次分歧
    elif in_divergence and prev.get("available") and prev_state not in divergence_states:
        # 从"已知的非分歧态"确认切入分歧才 +1；跨数据缺口（prev 不可用）不臆断
        # 这是新一次分歧，保守结转，避免影子计数虚高。
        divergence_count = prev_count + 1
    else:
        divergence_count = prev_count

    prev_days = prev.get("days_in_state")
    if prev.get("available") and prev_state == state and isinstance(prev_days, int):
        days_in_state = prev_days + 1
    else:
        days_in_state = 1

    is_first_divergence = state in divergence_states and divergence_count == 1
    label = ms.get("dominant_label")
    return {
        "schema": SCHEMA,
        "asof": asof,
        "available": True,
        "dominant_state": state,
        "state_label": label,
        "previous_state": prev_state,
        "days_in_state": days_in_state,
        "divergence_count": divergence_count,
        "is_first_divergence": is_first_divergence,
        "cycle_phase": f"{label or state} D{days_in_state}",
        "notes": [],
    }


def shadow_constraints(
    memory: Mapping[str, Any] | None,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把周期记忆映射成**影子**约束——只描述若启用会拦什么，绝不真的拦。

    - 退潮(S6) 或 二次以上分歧 → would_block_new_positions（手册 E-05/E-02）
    - 分歧状态(S5) → would_downgrade_second_board_w2s（手册 R-03 分歧日弱转强不做）
    market_state 不可用时不给方向性约束（fail-closed，缺证据 != 允许）。
    """
    cfg = _config(config)
    mem = dict(memory or {})
    result = {
        "schema": "cycle_shadow_constraints_v1",
        "enabled": bool(cfg.get("enabled")),
        "shadow_only": True,
        "would_block_new_positions": False,
        "would_downgrade_second_board_w2s": False,
        "reasons": [],
    }
    if not mem.get("available") or not mem.get("dominant_state"):
        result["reasons"].append("周期状态不可用，影子层不产出方向性约束（fail-closed）")
        return result

    state = mem.get("dominant_state")
    count = int(mem.get("divergence_count") or 0)
    reasons: list[str] = []

    weaken_states = {str(s) for s in cfg.get("weaken_states") or []}
    divergence_states = {str(s) for s in cfg.get("divergence_states") or []}
    second_min = int(cfg.get("second_divergence_min") or 2)

    if state in weaken_states:
        result["would_block_new_positions"] = True
        reasons.append(f"退潮状态 {state}({mem.get('state_label')}) D{mem.get('days_in_state')}：影子拦截新开仓")
    if count >= second_min:
        result["would_block_new_positions"] = True
        reasons.append(f"二次以上分歧(计数{count}≥{second_min})：影子拦截新开仓")
    if state in divergence_states:
        result["would_downgrade_second_board_w2s"] = True
        which = "首次分歧" if mem.get("is_first_divergence") else f"第{count}次分歧"
        reasons.append(f"分歧状态 {state}({which})：影子降级二板弱转强为 research_only")

    result["reasons"] = reasons
    return result


# ========== 持久化（读上一日记忆 → 推进 → 写最新 + append 影子日志）==========

def read_cycle_memory() -> Optional[dict[str, Any]]:
    record = read_json(data_file(*_LATEST_FILE), None)
    return record if isinstance(record, dict) else None


def _append_shadow_log(record: Mapping[str, Any]) -> None:
    path = data_file(*_SHADOW_LOG)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with file_lock(path):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def record_cycle_state(
    market_state: Mapping[str, Any] | None,
    *,
    asof: str,
    index_trend: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """读上一日记忆 → 推进 → 落最新记忆 + append 一行影子日志。返回影子日志行。

    index_trend 是可选的指数趋势闸门输出（index_trend_gate），一并记进影子日志，
    供 P2 对照。本函数只写影子文件，不碰任何实盘信号/排序。
    """
    prev = read_cycle_memory()
    memory = advance_cycle_memory(prev, market_state, asof=asof, config=config)
    if memory.get("asof") == asof and memory is not prev:
        atomic_write_json(data_file(*_LATEST_FILE), memory)
    constraints = shadow_constraints(memory, config=config)
    shadow_row = {
        "schema": "cycle_shadow_row_v1",
        "asof": asof,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cycle_memory": memory,
        "cycle_shadow_constraints": constraints,
        "index_trend": dict(index_trend or {}),
    }
    _append_shadow_log(shadow_row)
    return shadow_row


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="市场周期记忆层（影子）")
    parser.add_argument("--show-latest", action="store_true", help="打印最新记忆记录")
    args = parser.parse_args()
    if args.show_latest:
        print(json.dumps(read_cycle_memory() or {"found": False}, ensure_ascii=False, indent=2))
    else:
        parser.print_help()
