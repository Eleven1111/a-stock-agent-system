#!/usr/bin/env python3
"""Render bounded morning intelligence from already-persisted stage outputs."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from typing import Any, Mapping

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

from paths import data_file  # noqa: E402
from state_store import read_json  # noqa: E402
import stage_intelligence  # noqa: E402


STAGE_PATHS = {
    "preopen": ("stock-triage", "candidate_pool_latest.json"),
    "auction": ("daban-stock-picker", "auction_shortlist_latest.json"),
    "open": ("daban-stock-picker", "open_confirmation_latest.json"),
}

STAGE_LABELS = {
    "preopen": "早盘情报简报",
    "auction": "集合竞价简报",
    "open": "开盘摘要",
}


def _label(item: Mapping[str, Any]) -> str:
    return f"{item.get('name') or item.get('code')}({item.get('code')})"


def _score(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        if item.get(key) is not None:
            return str(item.get(key))
    return "-"


def _sector_momentum_lines() -> list[str]:
    """从 signal_context 提取板块动量/轮动摘要（缺失/过期时静默省略）。"""
    try:
        from signal_context import read_signal_context

        ctx = read_signal_context() or {}
    except Exception:  # noqa: BLE001 — 简报缺一段不缺整份
        return []
    lines: list[str] = []
    momentum = ctx.get("sector_momentum") or {}
    hot = [
        f"{entry.get('name')}({entry.get('signal')})"
        for entry in momentum.get("sectors") or []
        if entry.get("signal") in ("strong", "emerging")
    ][:5]
    if hot:
        lines.append("板块动量：" + "、".join(hot))
    rotation = (ctx.get("sector_rotation") or {}).get("rotation_signal")
    if rotation:
        lines.append(f"板块轮动：{rotation}")
    return lines


def _degraded_lines(result: Mapping[str, Any], *, tail: str) -> list[str]:
    """降级警示行；未降级返回空列表。

    空榜单/无信号有两种含义：真的没有机会，或根本没采到数据。后者必须说出来，
    否则读者会把"没有观测"读成"没有行情"（issue #112 / #113）。
    """
    if str(result.get("status")) != "degraded":
        return []
    lines = [f"⚠️ 数据降级，{tail}"]
    reasons = "；".join(str(item) for item in result.get("degraded_reasons") or [])
    if reasons:
        lines.append(f"原因：{reasons}")
    return lines


def _auction_lines(result: Mapping[str, Any], asof: str) -> list[str]:
    digest = stage_intelligence.auction_digest(result)
    lines = [
        f"## 集合竞价简报 | {asof}",
        f"全市场有效竞价因子：{digest['full_market_factor_count']}",
    ]
    lines.extend(_degraded_lines(
        result,
        tail=f"collection_status={result.get('collection_status') or 'unknown'}，"
             "以下榜单不可用作决策依据",
    ))
    for title, items in (
        ("### 竞价涨幅 TOP", digest["market_gainers"]),
        ("### 竞价跌幅 TOP", digest["market_decliners"]),
    ):
        lines.append(title)
        for item in items:
            scope = "执行短名单" if item["in_execution_shortlist"] else "池外研究情报"
            lines.append(f"- {_label(item)}：{_score(item, 'auction_gap_pct')}%｜{scope}")
    if digest["high_daban_candidates"]:
        lines.append("### 打板评分≥90")
        lines.extend(
            f"- {_label(item)}：{_score(item, 'daban_score')}"
            for item in digest["high_daban_candidates"]
        )
    return lines


def _open_lines(result: Mapping[str, Any], asof: str) -> list[str]:
    digest = stage_intelligence.open_digest(result)
    temperature = result.get("market_temperature") or {}
    regime = result.get("market_regime") or {}
    lines = [
        f"## 开盘摘要 | {asof}",
        f"市场概况：温度={temperature.get('tier') or 'N/A'}｜"
        f"状态={regime.get('regime') or regime.get('label') or 'N/A'}",
    ]
    # 降级时档位仍如实显示（诊断线索），但必须点明风险预算已归零——
    # 否则"温度=发酵｜无可执行信号"会被读成"市场不错，只是今天没标的"。
    lines.extend(_degraded_lines(
        result, tail="上方温度档位不代表可参与，新仓已阻断",
    ))
    lines.append("### 门禁后信号")
    if digest["signals"]:
        lines.extend(
            f"- {_label(item)}：{item.get('decision') or item.get('action')}｜"
            f"开盘分{_score(item, 'open_score')}"
            for item in digest["signals"]
        )
    else:
        lines.append("- 无可执行信号")
    if digest["filtered_highlights"]:
        lines.append("### 被过滤高分票（研究情报）")
        for item in digest["filtered_highlights"]:
            reasons = "；".join(item.get("filter_reasons") or [])
            lines.append(
                f"- {_label(item)}：打板{_score(item, 'daban_score', 'auction_daban_score')}｜{reasons}"
            )
    return lines


def format_brief(stage: str, result: Mapping[str, Any], *, max_chars: int = 2400) -> str:
    asof = result.get("asof") or "unknown"
    lines: list[str] = []
    if stage == "preopen":
        digest = stage_intelligence.preopen_digest(result)
        lines.extend([
            f"## 早盘情报简报 | {asof}",
            f"全市场{digest['scanned_count']}｜合格{digest['eligible_count']}｜"
            f"深度池{digest['candidate_count']}｜09:24全市场竞价扫描{digest['auction_scan_count']}",
        ])
        lines.extend(_sector_momentum_lines())
        lines.append("### 打板评分 TOP")
        lines.extend(
            f"- {_label(item)}：{_score(item, 'daban_score')}"
            for item in digest["top_daban"]
        )
        lines.append("### 趋势评分 TOP")
        lines.extend(
            f"- {_label(item)}：{_score(item, 'trend_score')}"
            for item in digest["top_trend"]
        )
        if not digest["top_daban"] and not digest["top_trend"]:
            lines.append("")
            lines.append("⚠️ 极端弱市，所有候选已降级为 research_only，无可操作标的")
    elif stage == "auction":
        lines.extend(_auction_lines(result, asof))
    elif stage == "open":
        lines.extend(_open_lines(result, asof))
    else:
        raise ValueError(f"unknown stage: {stage}")
    text = "\n".join(lines).strip()
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


def load_stage(stage: str, *, asof: str) -> dict[str, Any]:
    skill, filename = STAGE_PATHS[stage]
    result = read_json(data_file(skill, filename), {})
    if not isinstance(result, dict) or result.get("status") != "ready":
        return {}
    if stage in {"auction", "open"} and str(result.get("asof") or "") != asof:
        return {}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=sorted(STAGE_PATHS), required=True)
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--max-chars", type=int, default=2400)
    args = parser.parse_args()
    result = load_stage(args.stage, asof=args.asof)
    if result:
        print(format_brief(args.stage, result, max_chars=args.max_chars))
    else:
        print(
            f"⚠️ {STAGE_LABELS[args.stage]}未生成 | {args.asof} | "
            "上游快照缺失或过期，请检查对应采集任务。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
