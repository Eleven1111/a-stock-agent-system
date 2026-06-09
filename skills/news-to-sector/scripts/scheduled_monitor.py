#!/usr/bin/env python3
"""
Scheduled news monitor with fixed query set.

This replaces Gateway-side prompt/template injection for cron. It fetches a
bounded SerpAPI news result set, then records concise candidate events for
stock-triage. If SerpAPI is unavailable, it fails closed without directional
advice.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "..", "common"))

from a_stock_http import load_hermes_env  # noqa: E402


DEFAULT_QUERIES = [
    "国务院 发改委 工信部 A股 产业政策",
    "半导体 封测 AI算力 A股 订单",
    "高温 电力 电网 空调 A股",
    "地缘冲突 能源 黄金 航运 A股",
]


def _serpapi_key() -> str | None:
    load_hermes_env()
    keys = os.environ.get("SERPAPI_KEYS") or os.environ.get("SERPAPI_API_KEY") or ""
    return next((k.strip() for k in keys.split(",") if k.strip()), None)


def fetch_news(query: str, api_key: str, limit: int) -> List[Dict[str, Any]]:
    params = urllib.parse.urlencode({
        "engine": "google_news",
        "q": query,
        "hl": "zh-cn",
        "gl": "cn",
        "api_key": api_key,
    })
    url = f"https://serpapi.com/search.json?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes A-Stock Agent"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    items = payload.get("news_results") or []
    events = []
    for item in items[:limit]:
        title = item.get("title") or ""
        snippet = item.get("snippet") or ""
        if not title and not snippet:
            continue
        events.append({
            "query": query,
            "title": title,
            "snippet": snippet,
            "source": (item.get("source") or {}).get("name") if isinstance(item.get("source"), dict) else item.get("source"),
            "date": item.get("date"),
            "link": item.get("link"),
        })
    return events


def run_monitor(queries: List[str], limit: int) -> Dict[str, Any]:
    api_key = _serpapi_key()
    if not api_key:
        return {
            "schema": "scheduled_news_monitor_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "insufficient_data",
            "message": "SERPAPI_API_KEY/SERPAPI_KEYS missing; no directional news judgement",
            "events": [],
            "signals": [],
        }

    events: List[Dict[str, Any]] = []
    errors = []
    for query in queries:
        try:
            events.extend(fetch_news(query, api_key, limit))
        except Exception as exc:
            errors.append({"query": query, "error": str(exc)})

    seen = set()
    deduped = []
    for event in events:
        key = event.get("link") or event.get("title")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)

    return {
        "schema": "scheduled_news_monitor_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "ready" if deduped else "no_signal",
        "query_count": len(queries),
        "events": deduped,
        "event_count": len(deduped),
        "signals": deduped,
        "signal_count": len(deduped),
        "errors": errors,
    }


def format_report(result: Dict[str, Any]) -> str:
    if result["status"] != "ready":
        return ""
    lines = [f"## 资讯监控 | {result['event_count']}条"]
    for event in result["events"][:8]:
        lines.append(f"- {event['title']} | {event.get('source') or 'unknown'}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cron-safe scheduled news monitor")
    parser.add_argument("--queries", help="逗号分隔查询词；默认使用A股固定监控词")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    queries = [q.strip() for q in args.queries.split(",") if q.strip()] if args.queries else DEFAULT_QUERIES
    result = run_monitor(queries, args.limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        report = format_report(result)
        if report:
            print(report)


if __name__ == "__main__":
    main()
