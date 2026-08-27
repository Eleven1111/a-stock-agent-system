#!/usr/bin/env python3
"""S4 盘前表产物的读写位置与消费纪律（NON-LIVE 研究层）。

构建端是 ``scripts/preleader_pretable_build.py``，消费端是
``scripts/strategy_shadow_runner.py``。两端都从这里取路径与读取规则，避免各写一份
"哪张表算数"的判断——那种重复一旦漂移，消费端会开始接受构建端认为不可用的表。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from paths import data_file
from state_store import read_json

SCHEMA = "preleader_pretable_artifact_v1"


def output_path(as_of: str) -> str:
    return data_file("stock-triage", os.path.join("preleader_pretable", f"{as_of}.json"))


def previous_trading_asof(asof: str) -> str | None:
    """找**严格早于** ``asof`` 的最近一张盘前表日期；没有则 None。

    按产物目录里的实际日期回溯，而不是按日历减一天：节假日与停摆日都不会有表，
    减一天会得到一个不存在的日期，然后把"作业停摆"误报成"表缺失"。
    """
    directory = Path(output_path(asof)).parent
    if not directory.is_dir():
        return None
    dates = sorted(
        path.stem for path in directory.glob("*.json")
        if len(path.stem) == 10 and path.stem < str(asof)[:10]
    )
    return dates[-1] if dates else None


def load_pretable(as_of: str) -> tuple[Mapping[str, Any] | None, str]:
    """读 ``as_of`` 当日的盘前表；只有 ``status == "ok"`` 才交出表体。

    返回 ``(pretable, reason)``——取不到时 ``pretable`` 为 None 且 reason 说明原因，
    调用方据此报 unavailable，而不是拿一张退化的表当有效表用。
    """
    payload = read_json(output_path(as_of), None)
    if not isinstance(payload, Mapping):
        return None, "pretable_artifact_missing"
    if payload.get("status") != "ok":
        gaps = ",".join(payload.get("evidence_gaps") or []) or "unknown"
        return None, f"pretable_degraded:{gaps}"
    pretable = payload.get("pretable")
    if not isinstance(pretable, Mapping):
        return None, "pretable_body_missing"
    return pretable, "ok"


def load_previous_pretable(asof: str) -> tuple[Mapping[str, Any] | None, str]:
    """取 D-1 盘前表。

    S4 的成败点是"表必须是 D-1 晚间产物"，所以只找**严格早于** ``asof`` 的那张，
    绝不回退到当日：拿当日的表判当日等于用 D0 信息选样本，
    ``preleader_arbitrage`` 的 ``COND_PRETABLE_FRESH`` 也会把它判掉。
    """
    previous = previous_trading_asof(asof)
    if not previous:
        return None, "no_prior_pretable_artifact"
    return load_pretable(previous)
