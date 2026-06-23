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


def _label(item: Mapping[str, Any]) -> str:
    return f"{item.get('name') or item.get('code')}({item.get('code')})"


def _score(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        if item.get(key) is not None:
            return str(item.get(key))
    return "-"


def format_brief(stage: str, result: Mapping[str, Any], *, max_chars: int = 2400) -> str:
    asof = result.get("asof") or "unknown"
    lines: list[str] = []
    if stage == "preopen":
        digest = stage_intelligence.preopen_digest(result)
        lines.extend([
            f"## 早盘情报简报 | {asof}",
            f"全市场{digest['scanned_count']}｜合格{digest['eligible_count']}｜"
            f"深度池{digest['candidate_count']}｜09:24全市场竞价扫描{digest['auction_scan_count']}",
            "### 打板评分 TOP",
        ])
        lines.extend(
            f"- {_label(item)}：{_score(item, 'daban_score')}"
            for item in digest["top_daban"]
        )
        lines.append("### 趋势评分 TOP")
        lines.extend(
            f"- {_label(item)}：{_score(item, 'trend_score')}"
            for item in digest["top_trend"]
        )
    elif stage == "auction":
        digest = stage_intelligence.auction_digest(result)
        lines.extend([
            f"## 集合竞价简报 | {asof}",
            f"全市场有效竞价因子：{digest['full_market_factor_count']}",
            "### 竞价涨幅 TOP",
        ])
        for item in digest["market_gainers"]:
            scope = "执行短名单" if item["in_execution_shortlist"] else "池外研究情报"
            lines.append(f"- {_label(item)}：{_score(item, 'auction_gap_pct')}%｜{scope}")
        lines.append("### 竞价跌幅 TOP")
        for item in digest["market_decliners"]:
            scope = "执行短名单" if item["in_execution_shortlist"] else "池外研究情报"
            lines.append(f"- {_label(item)}：{_score(item, 'auction_gap_pct')}%｜{scope}")
        if digest["high_daban_candidates"]:
            lines.append("### 打板评分≥90")
            lines.extend(
                f"- {_label(item)}：{_score(item, 'daban_score')}"
                for item in digest["high_daban_candidates"]
            )
    elif stage == "open":
        digest = stage_intelligence.open_digest(result)
        temperature = result.get("market_temperature") or {}
        regime = result.get("market_regime") or {}
        lines.extend([
            f"## 开盘摘要 | {asof}",
            f"市场概况：温度={temperature.get('tier') or 'N/A'}｜"
            f"状态={regime.get('regime') or regime.get('label') or 'N/A'}",
            "### 门禁后信号",
        ])
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
