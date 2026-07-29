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
import site
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
site.addsitedir(COMMON)

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


def _append_overview(lines: list[str], agg: dict[str, Any], seen_count: int) -> None:
    lines.extend([
        "## 📊 管道概览",
        "",
        f"- **扫描轮次**: {agg['run_count']}",
        f"- **采集总量**: {agg['total_scanned']} 条",
        f"- **L1 评分**: {agg['total_l1_scored']} 条",
        f"- **L1 通过**: {agg['total_l1_passed']} 条",
        f"- **L1 拒绝**: {agg['total_l1_rejected']} 条",
        f"- **去重过滤**: {agg['total_duplicate']} 条",
        f"- **新入队列**: {agg['total_enqueued']} 条",
        f"- **累计指纹库**: {seen_count} 条",
        "",
    ])


def _append_queue_status(lines: list[str], queue_items: list[dict[str, Any]]) -> None:
    if not queue_items:
        return
    labels = {
        "pending": "⏳ 待L2分级",
        "claimed": "🔄 L2处理中",
        "graded": "✅ 已分级",
        "expired": "⏰ 已过期",
    }
    lines.extend(["## 📋 队列状态（当日条目）", ""])
    for status, count in classify_queue_by_status(queue_items).items():
        lines.append(f"- {labels.get(status, status)}: {count}")
    lines.append("")


def _append_distributions(lines: list[str], items: list[dict[str, Any]]) -> None:
    if not items:
        return
    source_counts = classify_by_source(items)
    if source_counts:
        lines.extend(["## 📡 来源分布", ""])
        max_count = max(source_counts.values())
        lines.extend(
            _format_source_bar(name, count, max_count)
            for name, count in source_counts.items()
        )
        lines.append("")
    rank_counts = classify_by_source_rank(items)
    if rank_counts:
        lines.extend(["## 🏛️ 信源等级分布", ""])
        lines.extend(
            f"- **{rank}**（{RANK_LABELS.get(rank, rank)}）: {count} 条"
            for rank, count in rank_counts.items()
        )
        lines.append("")
    tier_counts = classify_by_keyword_tier(items)
    if tier_counts:
        lines.extend(["## 🏷️ 主题分级", ""])
        lines.extend(
            f"- {TIER_LABELS.get(tier, tier)}: {count} 条"
            for tier, count in tier_counts.items()
        )
        lines.append("")
    keyword_counts = classify_by_keywords(items)
    if keyword_counts:
        lines.extend(["## 🔑 高频关键词 TOP15", ""])
        lines.extend(
            f"- **{keyword}**: {count} 次"
            for keyword, count in keyword_counts.items()
        )
        lines.append("")


def _append_failures(lines: list[str], agg: dict[str, Any]) -> None:
    failed_source_ids = agg.get("failed_source_ids")
    if not failed_source_ids:
        return
    lines.extend(["## ⚠️ 采集失败源", ""])
    lines.extend(f"- `{source_id}`" for source_id in failed_source_ids)
    lines.append("")


def _append_news_item(lines: list[str], index: int, item: dict[str, Any]) -> None:
    title = item.get("title") or item.get("excerpt") or "(无标题)"
    source = item.get("source_name") or item.get("source") or "未知"
    rank = item.get("source_rank") or ""
    collected = item.get("collected_at") or ""
    if "T" in collected:
        collected = collected.split("T")[1][:8]
    keywords = ", ".join(item.get("matched_keywords") or [])
    tier = item.get("keyword_tier") or ""
    status = item.get("status") or ""
    tier_icon = {"critical": "🔴", "high": "🟡", "medium": "🟢"}.get(tier, "⚪")
    lines.extend([f"### {index}. {title}", f"- 来源: {source} ({rank})"])
    if collected:
        lines.append(f"- 时间: {collected}")
    if keywords:
        lines.append(f"- 关键词: {tier_icon} {keywords}")
    if status:
        labels = {
            "pending": "待L2",
            "claimed": "L2处理中",
            "graded": "已分级",
            "expired": "已过期",
        }
        lines.append(f"- 状态: {labels.get(status, status)}")
    url = item.get("url") or ""
    fingerprint = item.get("fingerprint") or ""
    if url:
        lines.append(f"- 链接: {url}")
    elif fingerprint:
        lines.append(f"- 标识: `{fingerprint[:16]}…`")
    lines.append("")


def _append_news_items(
    lines: list[str], items: list[dict[str, Any]], max_items: int
) -> None:
    display_items = items[:max_items]
    if not display_items:
        lines.extend(["## 📰 当日处理新闻", "", "当日无新闻进入处理管道。", ""])
        return
    lines.extend([
        f"## 📰 当日处理新闻（前 {len(display_items)}/{len(items)} 条）",
        "",
    ])
    for index, item in enumerate(display_items, 1):
        _append_news_item(lines, index, item)


def format_markdown(
    target_date: str,
    agg: dict[str, Any],
    queue_items: list[dict[str, Any]],
    seen_count: int,
    *,
    max_items: int = 30,
) -> str:
    """Render the daily news brief as Markdown."""
    lines = [f"# 📰 新闻处理日报 | {target_date}", ""]
    _append_overview(lines, agg, seen_count)
    _append_queue_status(lines, queue_items)
    all_items = agg.get("new_items") or queue_items
    _append_distributions(lines, all_items)
    _append_failures(lines, agg)
    _append_news_items(lines, all_items, max_items)
    lines.extend(["---", f"*生成时间: {datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S')} CST*"])

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
