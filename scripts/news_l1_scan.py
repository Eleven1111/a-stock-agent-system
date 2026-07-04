#!/usr/bin/env python3
"""RSS/HTML primary-source collection + L1 deterministic rule scan.

Entry point for the ``news-l1-scan`` cron job. Fetches every source in
``config/news_pipeline.json`` through ``skills/common/news_sources.py``,
scores each collected item with the L1 rule engine in
``skills/common/news_pipeline.py`` (source-rank weight + keyword hits, zero
model cost), de-duplicates against the pipeline's own seen-set, and appends
newly-passed items to the L1 queue that ``scripts/news_grader.py`` consumes.

Silent by design: intraday polling runs many times a day and should produce
no output when nothing passed L1, matching the existing
``official-policy-watch`` / ``news-monitor-intraday`` convention.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
if COMMON not in sys.path:
    sys.path.insert(0, COMMON)

import news_pipeline  # noqa: E402
import news_sources  # noqa: E402


DEFAULT_CONFIG_PATH = os.path.join(ROOT, "config", "news_pipeline.json")


def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def run_scan(
    config: dict[str, Any],
    *,
    timeout: float,
    max_items_per_source: int,
) -> dict[str, Any]:
    checked_at = news_pipeline.now_bj_iso()
    source_results = news_sources.fetch_all(
        config, timeout=timeout, max_items_per_source=max_items_per_source,
    )
    ok_sources = [r for r in source_results if r["status"] == "ok"]
    failed_sources = [r for r in source_results if r["status"] != "ok"]

    collected: list[dict[str, Any]] = []
    for source_result in source_results:
        collected.extend(source_result.get("items") or [])

    l1_config = config.get("l1") or {}
    l1_result = news_pipeline.run_l1_scan(collected, l1_config)
    fresh, duplicate_count = news_pipeline.dedupe_items(
        l1_result["passed"],
        max_seen=int(l1_config.get("queue_max_entries", 2000)) * 3,
    )
    enqueued = news_pipeline.enqueue_l1_items(
        fresh,
        queue_max_entries=int(l1_config.get("queue_max_entries", 2000)),
        now=checked_at,
    )

    status = "no_signal"
    if not ok_sources:
        status = "insufficient_source"
    elif enqueued:
        status = "ready"
    elif l1_result["passed"]:
        status = "no_new_signal"

    result = {
        "schema": "news_l1_scan_v1",
        "checked_at": checked_at,
        "status": status,
        "has_signal": enqueued > 0,
        "research_only": True,
        "trading_action": "none",
        "summary": {
            "ok_sources": len(ok_sources),
            "failed_sources": len(failed_sources),
            "collected_count": len(collected),
            "l1_scored": l1_result["scored"],
            "l1_passed": len(l1_result["passed"]),
            "l1_rejected": l1_result["rejected_count"],
            "duplicate_count": duplicate_count,
            "enqueued_count": enqueued,
        },
        "queue": news_pipeline.queue_summary(),
        "failed_source_ids": [r["source_id"] for r in failed_sources],
        "new_items": fresh[:25],
    }
    if status == "insufficient_source":
        result["blocked_reason"] = "all news pipeline sources failed"
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--silent", action="store_true", help="仅在有信号时输出")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--max-items-per-source", type=int, default=30)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config = load_config(args.config)
    result = run_scan(
        config, timeout=args.timeout, max_items_per_source=args.max_items_per_source,
    )
    news_pipeline.persist_l1_run(result)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif result["has_signal"]:
        print(f"📡 新闻L1扫描 | {result['checked_at']}")
        print(
            f"扫描{result['summary']['collected_count']}条 | "
            f"通过{result['summary']['l1_passed']}条 | "
            f"新增{result['summary']['enqueued_count']}条待L2分级"
        )
        for item in result["new_items"][:10]:
            print(f"  [{item.get('source_rank')}] {item.get('title')}")
    elif not args.silent:
        print(f"📡 新闻L1扫描 | {result['checked_at']} — 无新信号")
    return 1 if result["status"] == "insufficient_source" else 0


if __name__ == "__main__":
    raise SystemExit(main())
