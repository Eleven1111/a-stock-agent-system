#!/usr/bin/env python3
"""每日新闻处理早报 — 汇总当天进入新闻处理流程的全量数据。

读取 news-pipeline 的 L1 扫描运行记录和 L1 队列，生成用户可读的
Markdown 报告，涵盖：
  - 日期与扫描轮次
  - 采集/L1/L2 各阶段统计（通过、拒绝、去重、失败）
  - 按来源统计
  - 按关键词主题统计
  - 实际进入流程的新闻条目（标题、来源、时间、链接）

Usage:
  python3 scripts/news_daily_brief.py                        # 今天
  python3 scripts/news_daily_brief.py --date 2026-07-15      # 指定日期
  python3 scripts/news_daily_brief.py --json                 # JSON 输出
  python3 scripts/news_daily_brief.py --max-items 50         # 条目上限
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
if COMMON not in sys.path:
    sys.path.insert(0, COMMON)

from paths import skill_data_dir  # noqa: E402
from state_store import read_json  # noqa: E402

BJ = timezone(timedelta(hours=8))
SKILL = "news-pipeline"


# ── Data loading ─────────────────────────────────────────────────────────


def _l1_runs_dir() -> str:
    return os.path.join(skill_data_dir(SKILL), "l1_runs")


def _l1_queue_path() -> str:
    return os.path.join(skill_data_dir(SKILL), "l1_queue.json")


def _l1_seen_path() -> str:
    return os.path.join(skill_data_dir(SKILL), "l1_seen.json")


def load_l1_runs_for_date(target_date: str) -> list[dict[str, Any]]:
    """Load all L1 scan run artifacts for a given date (YYYY-MM-DD)."""
    day_dir = os.path.join(_l1_runs_dir(), target_date)
    if not os.path.isdir(day_dir):
        return []
    runs: list[dict[str, Any]] = []
    for filename in sorted(os.listdir(day_dir)):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(day_dir, filename)
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                runs.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    return runs


def load_l1_queue_items_for_date(target_date: str) -> list[dict[str, Any]]:
    """Load L1 queue items collected on the target date."""
    queue = read_json(_l1_queue_path(), [])
    if not isinstance(queue, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in queue:
        if not isinstance(entry, dict):
            continue
        collected_at = str(entry.get("collected_at") or "")
        if collected_at.startswith(target_date):
            items.append(entry)
    return items


def load_l1_seen_count() -> int:
    """Load the current seen-fingerprints count."""
    seen = read_json(_l1_seen_path(), {})
    fingerprints = seen.get("fingerprints") if isinstance(seen, dict) else []
    return len(fingerprints) if isinstance(fingerprints, list) else 0


# ── Aggregation ──────────────────────────────────────────────────────────


def aggregate_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate stats across all L1 scan runs for a day."""
    total_scanned = 0
    total_l1_scored = 0
    total_l1_passed = 0
    total_l1_rejected = 0
    total_duplicate = 0
    total_enqueued = 0
    ok_source_set: set[str] = set()
    failed_source_set: set[str] = set()
    new_items_all: list[dict[str, Any]] = []
    run_count = len(runs)

    for run in runs:
        summary = run.get("summary") or {}
        total_scanned += int(summary.get("collected_count") or 0)
        total_l1_scored += int(summary.get("l1_scored") or 0)
        total_l1_passed += int(summary.get("l1_passed") or 0)
        total_l1_rejected += int(summary.get("l1_rejected") or 0)
        total_duplicate += int(summary.get("duplicate_count") or 0)
        total_enqueued += int(summary.get("enqueued_count") or 0)

        # Source tracking from failed_source_ids
        for sid in (run.get("failed_source_ids") or []):
            failed_source_set.add(sid)

        # Collect new items from each run
        for item in (run.get("new_items") or []):
            new_items_all.append(item)

    return {
        "run_count": run_count,
        "total_scanned": total_scanned,
        "total_l1_scored": total_l1_scored,
        "total_l1_passed": total_l1_passed,
        "total_l1_rejected": total_l1_rejected,
        "total_duplicate": total_duplicate,
        "total_enqueued": total_enqueued,
        "failed_source_ids": sorted(failed_source_set),
        "new_items": new_items_all,
    }


def classify_by_source(items: list[dict[str, Any]]) -> dict[str, int]:
    """Count items by source_name."""
    counter: Counter = Counter()
    for item in items:
        source = item.get("source_name") or item.get("source") or "未知"
        counter[source] += 1
    return dict(counter.most_common())


def classify_by_keyword_tier(items: list[dict[str, Any]]) -> dict[str, int]:
    """Count items by keyword_tier (critical/high/medium)."""
    counter: Counter = Counter()
    for item in items:
        tier = item.get("keyword_tier") or "未分类"
        counter[tier] += 1
    return dict(counter.most_common())


def classify_by_keywords(items: list[dict[str, Any]]) -> dict[str, int]:
    """Count keyword occurrences across all items."""
    counter: Counter = Counter()
    for item in items:
        for kw in (item.get("matched_keywords") or []):
            counter[kw] += 1
    return dict(counter.most_common(15))


def classify_by_source_rank(items: list[dict[str, Any]]) -> dict[str, int]:
    """Count items by source_rank (S5/S4/S3/S2/S1)."""
    counter: Counter = Counter()
    for item in items:
        rank = item.get("source_rank") or "未知"
        counter[rank] += 1
    return dict(counter.most_common())


def classify_queue_by_status(items: list[dict[str, Any]]) -> dict[str, int]:
    """Count queue items by status (pending/claimed/graded/expired)."""
    counter: Counter = Counter()
    for item in items:
        status = item.get("status") or "未知"
        counter[status] += 1
    return dict(counter.most_common())


# ── Markdown formatting ──────────────────────────────────────────────────

TIER_LABELS = {
    "critical": "🔴 关键",
    "high": "🟡 重要",
    "medium": "🟢 关注",
}

RANK_LABELS = {
    "S5": "央级官方",
    "S4": "部委/监管",
    "S3": "官方通讯",
    "S2": "交易所/财经",
    "S1": "社交关注",
    "S0": "其他",
}


def _format_source_bar(name: str, count: int, max_count: int) -> str:
    bar_len = max(1, int(count / max(max_count, 1) * 12))
    bar = "█" * bar_len
    return f"  {name}　{bar} {count}"


def format_markdown(
    target_date: str,
    agg: dict[str, Any],
    queue_items: list[dict[str, Any]],
    seen_count: int,
    *,
    max_items: int = 30,
) -> str:
    """Render the daily news brief as Markdown."""
    lines: list[str] = []

    # Header
    lines.append(f"# 📰 新闻处理日报 | {target_date}")
    lines.append("")

    # ── Pipeline Overview ──
    lines.append("## 📊 管道概览")
    lines.append("")
    lines.append(f"- **扫描轮次**: {agg['run_count']}")
    lines.append(f"- **采集总量**: {agg['total_scanned']} 条")
    lines.append(f"- **L1 评分**: {agg['total_l1_scored']} 条")
    lines.append(f"- **L1 通过**: {agg['total_l1_passed']} 条")
    lines.append(f"- **L1 拒绝**: {agg['total_l1_rejected']} 条")
    lines.append(f"- **去重过滤**: {agg['total_duplicate']} 条")
    lines.append(f"- **新入队列**: {agg['total_enqueued']} 条")
    lines.append(f"- **累计指纹库**: {seen_count} 条")
    lines.append("")

    # ── Queue Status ──
    if queue_items:
        status_counts = classify_queue_by_status(queue_items)
        lines.append("## 📋 队列状态（当日条目）")
        lines.append("")
        status_labels = {
            "pending": "⏳ 待L2分级",
            "claimed": "🔄 L2处理中",
            "graded": "✅ 已分级",
            "expired": "⏰ 已过期",
        }
        for status, count in status_counts.items():
            label = status_labels.get(status, status)
            lines.append(f"- {label}: {count}")
        lines.append("")

    # ── Source Distribution ──
    all_items = agg.get("new_items") or queue_items
    if all_items:
        source_counts = classify_by_source(all_items)
        if source_counts:
            lines.append("## 📡 来源分布")
            lines.append("")
            max_count = max(source_counts.values()) if source_counts else 1
            for name, count in source_counts.items():
                lines.append(_format_source_bar(name, count, max_count))
            lines.append("")

    # ── Source Rank Distribution ──
    if all_items:
        rank_counts = classify_by_source_rank(all_items)
        if rank_counts:
            lines.append("## 🏛️ 信源等级分布")
            lines.append("")
            for rank, count in rank_counts.items():
                label = RANK_LABELS.get(rank, rank)
                lines.append(f"- **{rank}**（{label}）: {count} 条")
            lines.append("")

    # ── Keyword Tier Distribution ──
    if all_items:
        tier_counts = classify_by_keyword_tier(all_items)
        if tier_counts:
            lines.append("## 🏷️ 主题分级")
            lines.append("")
            for tier, count in tier_counts.items():
                label = TIER_LABELS.get(tier, tier)
                lines.append(f"- {label}: {count} 条")
            lines.append("")

    # ── Top Keywords ──
    if all_items:
        kw_counts = classify_by_keywords(all_items)
        if kw_counts:
            lines.append("## 🔑 高频关键词 TOP15")
            lines.append("")
            for kw, count in kw_counts.items():
                lines.append(f"- **{kw}**: {count} 次")
            lines.append("")

    # ── Failure Stats ──
    if agg.get("failed_source_ids"):
        lines.append("## ⚠️ 采集失败源")
        lines.append("")
        for sid in agg["failed_source_ids"]:
            lines.append(f"- `{sid}`")
        lines.append("")

    # ── Actual News Items ──
    display_items = all_items[:max_items]
    if display_items:
        lines.append(f"## 📰 当日处理新闻（前 {len(display_items)}/{len(all_items)} 条）")
        lines.append("")
        for idx, item in enumerate(display_items, 1):
            title = item.get("title") or item.get("excerpt") or "(无标题)"
            source = item.get("source_name") or item.get("source") or "未知"
            rank = item.get("source_rank") or ""
            collected = item.get("collected_at") or ""
            # Show only time part if full datetime
            if "T" in collected:
                collected = collected.split("T")[1][:8]
            url = item.get("url") or ""
            fp = item.get("fingerprint") or ""
            keywords = ", ".join(item.get("matched_keywords") or [])
            tier = item.get("keyword_tier") or ""
            status = item.get("status") or ""

            tier_icon = {"critical": "🔴", "high": "🟡", "medium": "🟢"}.get(tier, "⚪")

            lines.append(f"### {idx}. {title}")
            lines.append(f"- 来源: {source} ({rank})")
            if collected:
                lines.append(f"- 时间: {collected}")
            if keywords:
                lines.append(f"- 关键词: {tier_icon} {keywords}")
            if status:
                status_labels_item = {
                    "pending": "待L2",
                    "claimed": "L2处理中",
                    "graded": "已分级",
                    "expired": "已过期",
                }
                lines.append(f"- 状态: {status_labels_item.get(status, status)}")
            if url:
                lines.append(f"- 链接: {url}")
            elif fp:
                lines.append(f"- 标识: `{fp[:16]}…`")
            lines.append("")
    else:
        lines.append("## 📰 当日处理新闻")
        lines.append("")
        lines.append("当日无新闻进入处理管道。")
        lines.append("")

    # Footer
    lines.append("---")
    now_str = datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"*生成时间: {now_str} CST*")

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────


def build_daily_brief(
    target_date: str,
    *,
    max_items: int = 30,
) -> dict[str, Any]:
    """Build the daily news brief data structure."""
    runs = load_l1_runs_for_date(target_date)
    queue_items = load_l1_queue_items_for_date(target_date)
    seen_count = load_l1_seen_count()
    agg = aggregate_runs(runs)

    return {
        "schema": "news_daily_brief_v1",
        "date": target_date,
        "aggregate": agg,
        "queue_items_count": len(queue_items),
        "seen_fingerprints_count": seen_count,
        "queue_items": queue_items,
        "markdown": format_markdown(
            target_date, agg, queue_items, seen_count, max_items=max_items,
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="目标日期 YYYY-MM-DD（默认今天）",
    )
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--max-items", type=int, default=30, help="新闻条目上限")
    args = parser.parse_args()

    result = build_daily_brief(args.date, max_items=args.max_items)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(result["markdown"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
