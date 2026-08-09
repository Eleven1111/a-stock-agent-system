#!/usr/bin/env python3
"""
盘中实时催化触发器 — T1 级催化 15 分钟响应
==========================================
解决核心时效断点：原系统催化面只在 15:18 批量跑一次，
盘中突发政策/重大事件需等到次日才评估。

本脚本每 15 分钟运行一次（09:45 ~ 14:45），逻辑：
  1. 扫描 news_monitor_v3 推送队列中的新催化事件
  2. 快速分级（T1 国家战略/重大 → T2 订单业绩 → T3 泛利好）
  3. T1 级催化立即更新 catalyst_context 缓存
  4. 命中候选池已有标的 → 推送盘中异动通知
  5. 命中新标的 → 写入 monitor_registry 观察列表

Usage:
  python3 realtime_catalyst_trigger.py
  python3 realtime_catalyst_trigger.py --json
  python3 realtime_catalyst_trigger.py --force  # 不检查交易时段
"""

import json
import os
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
from a_share_rules import add_trading_days  # noqa: E402
from a_stock_http import load_hermes_env  # noqa: E402
from catalyst_context import update_catalyst_context  # noqa: E402
from data_provider import fetch_serper_news  # noqa: E402
from data_provider import _next_serper_key  # noqa: E402
from http_client import DataSourceError  # noqa: E402
from paths import data_file, cache_dir  # noqa: E402
from state_store import read_json, atomic_write_json  # noqa: E402
import novelty_gate  # noqa: E402
import monitor_registry  # noqa: E402

T1_KEYWORDS = [
    "国家战略", "政策支持", "国产替代", "重大重组", "战略合作",
    "补贴", "纳入指数", "重大突破", "制裁", "退市",
    "立案调查", "财务造假",
]
T2_KEYWORDS = [
    "业绩增长", "业绩预增", "中标", "大额订单", "订单",
    "产能释放", "扩产", "回购", "增持", "涨价",
    "减持", "亏损", "诉讼", "处罚", "业绩下滑",
]

CANDIDATE_POOL = data_file("stock-triage", "candidate_pool_latest.json")
TRIGGER_CACHE = os.path.join(cache_dir("stock-triage"), "catalyst_trigger_cache.json")


class CatalystScan(list):
    def __init__(self, events=(), *, status="ready", errors=None):
        super().__init__(events)
        self.status = status
        self.errors = list(errors or [])


def in_trading_session(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 30) <= t <= dtime(15, 0)


def classify_catalyst(title: str, snippet: str = "") -> Optional[str]:
    text = f"{title} {snippet}"
    for kw in T1_KEYWORDS:
        if kw in text:
            return "T1"
    for kw in T2_KEYWORDS:
        if kw in text:
            return "T2"
    return None


def _normalize_stock_code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith(("sh", "sz")):
        text = text[2:]
    return text.zfill(6) if text.isdigit() else text


def scan_fresh_catalysts() -> List[Dict[str, Any]]:
    """从 Serper.dev 抓取最近新闻并分级。"""
    load_hermes_env()
    api_key = _next_serper_key()
    if not api_key:
        return CatalystScan(status="insufficient_data", errors=[{
            "source": "serper",
            "error_type": "missing_credential",
            "error": "SERPER_API_KEY/SERPER_API_KEYS missing",
        }])
    queries = [
        "A股 政策 重大 最新",
        "A股 涨停 板块 异动",
        "上市公司 公告 业绩 订单",
    ]
    seen_links = set()
    results = []
    errors = []
    successful_queries = 0
    for query in queries:
        try:
            items = fetch_serper_news(query, api_key, 5)
            successful_queries += 1
            for item in items.data if hasattr(items, "data") else []:
                link = item.get("link", "")
                if link in seen_links:
                    continue
                seen_links.add(link)
                title = item.get("title", "")
                snippet = item.get("snippet", "")
                tier = classify_catalyst(title, snippet)
                if tier:
                    results.append({
                        "title": title,
                        "snippet": snippet,
                        "source": item.get("source", ""),
                        "date": item.get("date", ""),
                        "link": link,
                        "tier": tier,
                        "scanned_at": datetime.now().isoformat(),
                    })
        except (DataSourceError, AttributeError, TypeError) as exc:
            errors.append({"query": query, "source": "serper", "error": str(exc)})
            continue
    status = "ready" if successful_queries else "insufficient_data"
    return CatalystScan(results, status=status, errors=errors)


def match_candidate_pool(catalysts: List[Dict]) -> Dict[str, List[Dict]]:
    """将催化事件匹配到候选池中的标的。"""
    pool = read_json(CANDIDATE_POOL, [])
    if isinstance(pool, dict):
        pool = pool.get("candidates") or []
    elif not isinstance(pool, list):
        pool = []
    name_to_codes: Dict[str, str] = {}
    for item in pool:
        if isinstance(item, dict) and item.get("name") and item.get("code"):
            name_to_codes[item["name"]] = item["code"]

    matched: Dict[str, List[Dict]] = {}
    for cat in catalysts:
        text = f"{cat.get('title', '')} {cat.get('snippet', '')}"
        for stock_name, code in name_to_codes.items():
            normalized = _normalize_stock_code(code)
            if stock_name in text or normalized in text:
                matched.setdefault(normalized, []).append({
                    **cat,
                    "stock_code": normalized,
                    "stock_name": stock_name,
                })
    return matched


def _coded_catalysts(catalysts: List[Dict]) -> List[Dict[str, Any]]:
    coded = []
    for cat in catalysts:
        code = _normalize_stock_code(cat.get("stock_code") or cat.get("code"))
        if not code or not code.isdigit():
            continue
        name = str(cat.get("stock_name") or cat.get("name") or code)
        coded.append({**cat, "stock_code": code, "stock_name": name})
    return coded


def run_trigger(force: bool = False) -> Dict[str, Any]:
    now = datetime.now()
    if not force and not in_trading_session(now):
        return {"status": "skipped", "reason": "非交易时段"}

    cache = read_json(TRIGGER_CACHE, {})
    today = now.strftime("%Y%m%d")
    if cache.get("_date") != today:
        cache = {"_date": today, "processed_links": []}

    scan = scan_fresh_catalysts()
    catalysts = list(scan)
    scan_status = getattr(scan, "status", "ready")
    scan_errors = list(getattr(scan, "errors", []))
    if scan_status == "insufficient_data":
        return {
            "status": "insufficient_data",
            "timestamp": now.isoformat(),
            "scanned": 0,
            "new": 0,
            "errors": scan_errors,
        }
    processed = set(cache.get("processed_links", []))
    new_catalysts = [c for c in catalysts if c.get("link") not in processed]

    if not new_catalysts:
        return {
            "status": "no_new",
            "timestamp": now.isoformat(),
            "scanned": len(catalysts),
            "new": 0,
        }
    novelty = novelty_gate.filter_items(
        new_catalysts,
        namespace="market-news",
        job_id="catalyst-trigger",
        now=now,
    )
    original_new_count = len(new_catalysts)
    new_catalysts = novelty.items

    if not new_catalysts:
        for c in novelty.duplicate_items:
            if c.get("link"):
                processed.add(c["link"])
        cache["processed_links"] = list(processed)[-500:]
        atomic_write_json(TRIGGER_CACHE, cache)
        return {
            "status": "no_new",
            "timestamp": now.isoformat(),
            "scanned": len(catalysts),
            "new": 0,
            "duplicate_event_count": novelty.duplicate_count,
            "archive_note": novelty_gate.duplicate_archive_note(novelty),
            "novelty_gate": {
                "fail_open": novelty.fail_open,
                "shadow": novelty.shadow,
                "would_suppress": novelty.would_suppress,
            },
        }

    t1_catalysts = [c for c in new_catalysts if c.get("tier") == "T1"]
    t2_catalysts = [c for c in new_catalysts if c.get("tier") == "T2"]

    matched = match_candidate_pool(new_catalysts)
    matched_events = [event for events in matched.values() for event in events]
    coded_events = _coded_catalysts(new_catalysts)
    matched_codes = set(matched)
    new_watch_events = [
        event for event in coded_events
        if event["stock_code"] not in matched_codes
    ]
    context_events = matched_events + new_watch_events
    if context_events:
        update_catalyst_context(context_events, generated_at=now)

    activated = []
    trading_date = now.date().isoformat()
    batch_id = f"realtime-catalyst-{trading_date.replace('-', '')}"
    event_expiry = add_trading_days(now.date(), 1)
    for event in new_watch_events:
        outcome = monitor_registry.activate(
            "stock",
            event["stock_code"],
            event["stock_name"],
            source="realtime_catalyst_trigger",
            expires_at=event_expiry,
            source_group="event_watch",
            trading_date=trading_date,
            batch_id=batch_id,
            metadata={
                "tier": event.get("tier"),
                "event_title": event.get("title"),
                "event_link": event.get("link"),
            },
        )
        if outcome.get("changed"):
            activated.append(event["stock_code"])

    alerts = []
    for code, events in matched.items():
        top_tier = min(e.get("tier", "T3") for e in events)
        alerts.append({
            "code": code,
            "name": events[0].get("stock_name", code),
            "tier": top_tier,
            "events": [e.get("title", "")[:50] for e in events[:3]],
            "action": "priority_watch" if top_tier == "T1" else "watch",
        })

    for c in [*new_catalysts, *novelty.duplicate_items]:
        if c.get("link"):
            processed.add(c["link"])
    cache["processed_links"] = list(processed)[-500:]
    atomic_write_json(TRIGGER_CACHE, cache)

    return {
        "status": "ok",
        "timestamp": now.isoformat(),
        "scanned": len(catalysts),
        "new": len(new_catalysts),
        "candidate_new": original_new_count,
        "duplicate_event_count": novelty.duplicate_count,
        "archive_note": novelty_gate.duplicate_archive_note(novelty),
        "novelty_gate": {
            "fail_open": novelty.fail_open,
            "shadow": novelty.shadow,
            "would_suppress": novelty.would_suppress,
        },
        "t1_count": len(t1_catalysts),
        "t2_count": len(t2_catalysts),
        "matched_stocks": len(matched),
        "new_watch_stocks": len(activated),
        "alerts": alerts,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="盘中催化触发器")
    p.add_argument("--json", action="store_true")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()

    result = run_trigger(force=args.force)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("status") == "ok":
            print(f"⚡ 催化扫描 | 新增{result['new']}条 (T1:{result['t1_count']} T2:{result['t2_count']})")
            for a in result.get("alerts", []):
                print(f"  {'🔴' if a['tier'] == 'T1' else '🟡'} {a['name']}({a['code']}) [{a['tier']}] → {a['action']}")
                for e in a.get("events", []):
                    print(f"    └─ {e}")
        elif result.get("status") == "no_new":
            print(f"催化扫描完成，无新事件 (共扫描{result['scanned']}条)")
            if result.get("archive_note"):
                print(result["archive_note"])
