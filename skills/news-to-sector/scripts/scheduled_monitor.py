#!/usr/bin/env python3
"""
Scheduled news monitor with macro baselines and dynamic runtime subscriptions.

This replaces Gateway-side prompt/template injection for cron. It fetches a
bounded Serper.dev news result set, then records concise candidate events for
stock-triage. If Serper.dev is unavailable, it fails closed without directional
advice.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "..", "common"))

from a_stock_http import load_hermes_env  # noqa: E402
from data_access_config import news_monitor_settings  # noqa: E402
from data_provider import fetch_serper_news  # noqa: E402
from data_provider import _next_serper_key as _serper_key  # noqa: E402
from http_client import DataSourceError  # noqa: E402
from monitor_registry import active_entries  # noqa: E402
from recommendation_quality import scan_announcement_risks  # noqa: E402
from catalyst_context import update_catalyst_context  # noqa: E402
from runtime_targets import load_stock_targets  # noqa: E402


_NEWS_CONFIG = news_monitor_settings()
DEFAULT_QUERIES = list(_NEWS_CONFIG["queries"])
DEFAULT_LIMIT = int(_NEWS_CONFIG["default_limit"])
DEFAULT_FRESHNESS_SLA_MINUTES = int(_NEWS_CONFIG.get("freshness_sla_minutes", 180))
INTRADAY_LIMIT = int(_NEWS_CONFIG.get("intraday_limit", DEFAULT_LIMIT))
INTRADAY_FRESHNESS_SLA_MINUTES = int(_NEWS_CONFIG.get("intraday_freshness_sla_minutes", 10))
INTRADAY_CANDIDATE_LIMIT = int(_NEWS_CONFIG.get("intraday_candidate_limit", 20))


def build_queries(base_queries: List[str] | None = None, *, mode: str = "scheduled") -> List[str]:
    queries = list(base_queries or DEFAULT_QUERIES)
    if mode == "intraday":
        for target in load_stock_targets(candidate_limit=INTRADAY_CANDIDATE_LIMIT):
            code = str(target.get("code") or "")
            label = str(target.get("name") or code)
            if code:
                queries.append(f"{label} {code} 异动公告 澄清 风险提示 监管问询 减持 停牌")
    for item in active_entries():
        kind = item.get("kind")
        key = str(item.get("key") or "")
        label = str(item.get("label") or key)
        if kind == "stock":
            if mode == "intraday":
                queries.append(f"{label} {key} 异动公告 澄清 风险提示 监管问询 减持 停牌")
            else:
                queries.append(f"{label} {key} 公告 澄清 风险提示 监管问询")
        elif kind in {"theme", "sector"}:
            queries.append(f"{label} A股 政策 产业链 订单 风险")
    return list(dict.fromkeys(query for query in queries if query.strip()))


def fetch_news(query: str, api_key: str, limit: int) -> List[Dict[str, Any]]:
    return fetch_serper_news(query, api_key, limit).data


def stock_code_from_query(query: str) -> str | None:
    match = re.search(r"(?<!\d)([036]\d{5})(?!\d)", query)
    return match.group(1) if match else None


def parse_news_time(value: Any, *, now: datetime | None = None) -> datetime | None:
    if not value:
        return None
    ref = now or datetime.now()
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"刚刚", "just now"}:
        return ref

    relative_patterns = [
        (r"(\d+(?:\.\d+)?)\s*minutes?\s+ago", "minutes"),
        (r"(\d+(?:\.\d+)?)\s*hours?\s+ago", "hours"),
        (r"(\d+(?:\.\d+)?)\s*days?\s+ago", "days"),
        (r"(\d+(?:\.\d+)?)\s*weeks?\s+ago", "weeks"),
        (r"(\d+(?:\.\d+)?)\s*months?\s+ago", "months"),
        (r"(\d+(?:\.\d+)?)\s*分钟前", "minutes"),
        (r"(\d+(?:\.\d+)?)\s*小时前", "hours"),
        (r"(\d+(?:\.\d+)?)\s*天前", "days"),
        (r"(\d+(?:\.\d+)?)\s*周前", "weeks"),
        (r"(\d+(?:\.\d+)?)\s*月前", "months"),
    ]
    for pattern, unit in relative_patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        amount = float(match.group(1))
        if unit == "minutes":
            delta = timedelta(minutes=amount)
        elif unit == "hours":
            delta = timedelta(hours=amount)
        elif unit == "days":
            delta = timedelta(days=amount)
        elif unit == "weeks":
            delta = timedelta(days=amount * 7)
        else:
            delta = timedelta(days=amount * 30)
        return ref - delta

    head = text.split(",")[0].strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(head, fmt)
        except ValueError:
            continue
    return None


def enrich_event_timing(
    event: Dict[str, Any],
    *,
    query: str,
    stock_code: str | None,
    now: datetime,
) -> Dict[str, Any]:
    enriched = dict(event)
    enriched["query"] = query
    if stock_code:
        enriched["stock_code"] = stock_code
    discovered_at = now.isoformat(timespec="seconds")
    enriched["discovered_at"] = discovered_at
    enriched["ingested_at"] = discovered_at
    published_at = parse_news_time(enriched.get("date"), now=now)
    if published_at is not None:
        latency = max(0, int((now - published_at).total_seconds() // 60))
        enriched["published_at"] = published_at.isoformat(timespec="seconds")
        enriched["latency_minutes"] = latency
    else:
        enriched["published_at"] = None
        enriched["latency_minutes"] = None
    return enriched


def evaluate_freshness(
    events: List[Dict[str, Any]],
    *,
    sla_minutes: int,
) -> Dict[str, Any]:
    latencies = [
        int(event["latency_minutes"])
        for event in events
        if isinstance(event.get("latency_minutes"), int)
    ]
    if not events:
        status = "no_events"
    elif not latencies:
        status = "unknown"
    elif min(latencies) <= sla_minutes:
        status = "fresh"
    else:
        status = "stale"
    return {
        "status": status,
        "sla_minutes": sla_minutes,
        "parsed_event_count": len(latencies),
        "unknown_event_count": len(events) - len(latencies),
        "newest_latency_minutes": min(latencies) if latencies else None,
        "oldest_latency_minutes": max(latencies) if latencies else None,
    }


def intraday_window_open(now: datetime | None = None) -> bool:
    ref = now or datetime.now()
    minute_of_day = ref.hour * 60 + ref.minute
    return (
        9 * 60 + 25 <= minute_of_day <= 11 * 60 + 30
        or 13 * 60 <= minute_of_day <= 14 * 60 + 55
    )


def classify_event(event: Dict[str, Any]) -> Dict[str, Any]:
    classified = dict(event)
    risk = scan_announcement_risks([event])
    classified["risk_classification"] = {
        "is_risk": bool(
            risk["thesis_invalidation_hits"]
            or risk["review_hits"]
            or risk["hard_risk_hits"]
        ),
        "clarification_hits": risk["clarification_hits"],
        "thesis_invalidation_hits": risk["thesis_invalidation_hits"],
        "review_hits": risk["review_hits"],
        "warning_only_hits": risk["warning_only_hits"],
        "hard_risk_hits": risk["hard_risk_hits"],
        "warnings": risk["warnings"],
    }
    latency = classified.get("latency_minutes")
    classified["freshness_class"] = (
        "unknown" if latency is None else "fresh"
    )
    return classified


def run_monitor(
    queries: List[str],
    limit: int,
    *,
    mode: str = "scheduled",
    freshness_sla_minutes: int | None = None,
    now: datetime | None = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
    sla = int(
        freshness_sla_minutes
        if freshness_sla_minutes is not None
        else (
            INTRADAY_FRESHNESS_SLA_MINUTES
            if mode == "intraday"
            else DEFAULT_FRESHNESS_SLA_MINUTES
        )
    )
    if mode == "intraday" and not intraday_window_open(current):
        return {
            "schema": "scheduled_news_monitor_v1",
            "generated_at": current.isoformat(timespec="seconds"),
            "status": "skipped",
            "mode": mode,
            "message": "outside A-share intraday news freshness window",
            "events": [],
            "signals": [],
            "freshness": evaluate_freshness([], sla_minutes=sla),
        }

    api_key = _serper_key()
    if not api_key:
        return {
            "schema": "scheduled_news_monitor_v1",
            "generated_at": current.isoformat(timespec="seconds"),
            "status": "insufficient_data",
            "mode": mode,
            "message": "SERPER_API_KEY missing; no directional news judgement",
            "events": [],
            "signals": [],
            "freshness": evaluate_freshness([], sla_minutes=sla),
        }

    events: List[Dict[str, Any]] = []
    errors = []
    for query in queries:
        try:
            stock_code = stock_code_from_query(query)
            for event in fetch_news(query, api_key, limit):
                events.append(
                    enrich_event_timing(
                        event,
                        query=query,
                        stock_code=stock_code,
                        now=current,
                    )
                )
        except DataSourceError as exc:
            errors.append({"query": query, **exc.to_dict()})
        except Exception as exc:
            errors.append({
                "query": query,
                "source": "serper",
                "error_type": "unexpected",
                "error": str(exc),
                "timestamp": current.isoformat(timespec="seconds"),
            })

    seen = set()
    deduped = []
    for event in events:
        key = event.get("link") or event.get("title")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(classify_event(event))

    risk_events = [
        event for event in deduped
        if (event.get("risk_classification") or {}).get("is_risk")
    ]
    freshness = evaluate_freshness(deduped, sla_minutes=sla)
    for event in deduped:
        latency = event.get("latency_minutes")
        if isinstance(latency, int):
            event["freshness_class"] = "fresh" if latency <= sla else "stale"
    update_catalyst_context(deduped)
    directional_ready = freshness["status"] == "fresh"
    status = "ready" if deduped else "no_signal"
    if deduped and not directional_ready:
        status = "stale_data"

    return {
        "schema": "scheduled_news_monitor_v1",
        "generated_at": current.isoformat(timespec="seconds"),
        "status": status,
        "mode": mode,
        "query_count": len(queries),
        "events": deduped,
        "event_count": len(deduped),
        "risk_events": risk_events,
        "risk_event_count": len(risk_events),
        "signals": deduped if directional_ready else [],
        "signal_count": len(deduped) if directional_ready else 0,
        "freshness": freshness,
        "errors": errors,
    }


def format_report(result: Dict[str, Any]) -> str:
    if result["status"] != "ready":
        return ""
    lines = [f"## 资讯监控 | {result['event_count']}条"]
    for event in result["events"][:8]:
        prefix = "⚠️ " if (event.get("risk_classification") or {}).get("is_risk") else ""
        lines.append(f"- {prefix}{event['title']} | {event.get('source') or 'unknown'}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cron-safe scheduled news monitor")
    parser.add_argument("--queries", help="逗号分隔查询词；默认使用宏观基线和动态监控订阅")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--mode", choices=["scheduled", "intraday"], default="scheduled")
    parser.add_argument("--freshness-sla-minutes", type=int)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    limit = args.limit if args.limit != DEFAULT_LIMIT or args.mode != "intraday" else INTRADAY_LIMIT
    queries = (
        [q.strip() for q in args.queries.split(",") if q.strip()]
        if args.queries
        else build_queries(mode=args.mode)
    )
    result = run_monitor(
        queries,
        limit,
        mode=args.mode,
        freshness_sla_minutes=args.freshness_sla_minutes,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        report = format_report(result)
        if report:
            print(report)


if __name__ == "__main__":
    main()
