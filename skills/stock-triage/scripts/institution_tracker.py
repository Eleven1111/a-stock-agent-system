#!/usr/bin/env python3
"""
机构行为追踪 — 调研/研报/增减持
===============================
数据源：东方财富数据中心（免费公开API）

Usage:
  python3 institution_tracker.py
  python3 institution_tracker.py --json
"""

import json
from datetime import datetime
from typing import Dict, List

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from data_provider import fetch_serper_news as _fetch_serper_news
from data_provider import _next_serper_key
from eastmoney_intelligence import (
    fetch_insider_trades as _fetch_insider_trades,
    fetch_reports,
    fetch_research_visits as _fetch_research_visits,
)
from http_client import DataSourceError
from fair_target_rotation import plan_fair_rotation, persist_rotation_cursor, rotation_metrics
from paths import data_file
from stock_intelligence import read_cache as read_stock_intelligence
import runtime_targets

# Production measurement (2026-08-30): six targets took 83.6s against a 90s
# cron budget.  Five preserves a real retry/jitter margin instead of treating a
# one-off pass six seconds below SIGTERM as healthy.
MAX_STOCK_TARGETS = 5
ROTATION_JOB_ID = "institution-weekly"


def rotation_cursor_file() -> str:
    return data_file("stock-triage", "scan_cursors/institution-weekly/cursor.json")


def runtime_target_plan() -> dict:
    return plan_fair_rotation(
        runtime_targets.load_stock_targets(),
        max_targets=MAX_STOCK_TARGETS,
        job_id=ROTATION_JOB_ID,
        cursor_path=rotation_cursor_file(),
    )


def load_runtime_targets() -> Dict[str, str]:
    return {
        target["code"]: target["name"]
        for target in runtime_target_plan()["targets"]
    }


def fetch_research_visits(code: str) -> List[Dict]:
    """机构调研（近30天）"""
    try:
        return _fetch_research_visits(code, page_size=5)
    except DataSourceError:
        return []


def fetch_analyst_reports(code: str) -> List[Dict]:
    """券商研报（近90天）"""
    try:
        return [{
            "date": item.get("date", ""),
            "org": item.get("institution", ""),
            "rating": item.get("rating", ""),
            "target_price": "",
            "title": item.get("title", ""),
            "pdf_url": item.get("pdf_url"),
            "eps_current_year": item.get("eps_current_year"),
            "eps_next_year": item.get("eps_next_year"),
            "eps_year_after_next": item.get("eps_year_after_next"),
        } for item in fetch_reports(code, page_size=5)]
    except DataSourceError:
        return []


def fetch_insider_trades(code: str) -> List[Dict]:
    """大股东增减持"""
    try:
        return _fetch_insider_trades(code, page_size=5)
    except DataSourceError:
        return []


def fetch_serper_inst_news(code: str, name: str) -> List[Dict]:
    """通过 Serper.dev 搜索机构相关新闻"""
    api_key = _next_serper_key()
    if not api_key:
        return []
    query = f"{name} {code} 券商研报 机构调研 评级 目标价"
    try:
        result = _fetch_serper_news(query, api_key, 3)
        return [
            {
                "title": item.get("title", ""),
                "source": item.get("source", ""),
                "date": item.get("date", ""),
            }
            for item in result.data
        ]
    except (DataSourceError, AttributeError, TypeError):
        return []


def collect_institution_data(targets: Dict[str, str] | None = None) -> Dict:
    plan = None
    if targets is None:
        plan = runtime_target_plan()
        targets = {
            target["code"]: target["name"]
            for target in plan["targets"]
        }
        target_metrics = rotation_metrics(plan)
    else:
        target_metrics = {
            "targets_total": len(targets),
            "targets_scanned": len(targets),
            "targets_deferred": 0,
            "priority_scanned": None,
            "cursor_before": None,
            "cursor_after": None,
            "cursor_state": "explicit_targets",
        }
    result = {
        "timestamp": datetime.now().isoformat(),
        "stocks": [],
        "alerts": [],
        **target_metrics,
        "targets_truncated": target_metrics["targets_deferred"],
    }

    for code, name in targets.items():
        stock = {"code": code, "name": name, "research_visits": [], "analyst_reports": [],
                 "insider_trades": [], "news": [], "market_intelligence": {}}

        intelligence = read_stock_intelligence(code)
        if intelligence.get("available"):
            stock["market_intelligence"] = intelligence
            for risk in intelligence.get("hard_risks") or []:
                result["alerts"].append({
                    "level": "🔴",
                    "msg": f"{name} 筹码硬风险: {risk}",
                })
            for warning in intelligence.get("warnings") or []:
                result["alerts"].append({
                    "level": "🟡",
                    "msg": f"{name} 筹码提示: {warning}",
                })

        # 机构调研
        visits = fetch_research_visits(code)
        if visits:
            stock["research_visits"] = visits
            if len(visits) >= 3:
                result["alerts"].append({
                    "level": "🟢",
                    "msg": f"{name} 近30天{len(visits)}次机构调研，关注度提升"
                })

        # 研报
        reports = fetch_analyst_reports(code)
        if reports:
            stock["analyst_reports"] = reports
            upgrades = [r for r in reports if "买入" in r.get("rating", "") or "增持" in r.get("rating", "")]
            if len(reports) >= 3:
                result["alerts"].append({
                    "level": "ℹ️",
                    "msg": f"{name} 近90天{len(reports)}篇研报，{len(upgrades)}篇看多"
                })

        # 增减持
        trades = fetch_insider_trades(code)
        if trades:
            stock["insider_trades"] = trades
            sells = [t for t in trades if t.get("direction") == "减持"]
            buys = [t for t in trades if t.get("direction") == "增持"]
            if sells:
                result["alerts"].append({
                    "level": "🟡",
                    "msg": f"⚠️ {name} 大股东减持，注意风险"
                })
            if buys:
                result["alerts"].append({
                    "level": "🟢",
                    "msg": f"{name} 大股东增持，信心信号"
                })

        # 新闻
        stock["news"] = fetch_serper_inst_news(code, name)

        result["stocks"].append(stock)

    if plan is not None:
        try:
            persist_rotation_cursor(plan)
            result["cursor_persisted"] = True
        except OSError as exc:
            result["cursor_persisted"] = False
            result["cursor_error"] = str(exc)

    return result


def format_report(data: Dict) -> str:
    lines = ["🏛️ **机构行为追踪**", f"⏰ {data['timestamp'][:16]}", ""]

    alerts = data.get("alerts", [])
    if alerts:
        lines.append("## ⚡ 机构信号")
        for a in alerts:
            lines.append(f"- {a['level']} {a['msg']}")
        lines.append("")

    for s in data.get("stocks", []):
        lines.append(f"### {s['name']}({s['code']})")

        if s.get("research_visits"):
            lines.append(f"  📋 近30天{s['research_visits'][0]['org_count']}家机构调研")

        if s.get("analyst_reports"):
            latest = s["analyst_reports"][0]
            target = f" 目标价{latest['target_price']}" if latest.get("target_price") else ""
            lines.append(f"  📝 最新研报: {latest['org']} {latest['rating']}{target} ({latest['date']})")

        if s.get("insider_trades"):
            for t in s["insider_trades"][:2]:
                lines.append(f"  {'🔺' if t['direction']=='增持' else '🔻'} {t['name']}{t['direction']} {t['shares']}股 ({t['date']})")

        if not any([s.get("research_visits"), s.get("analyst_reports"), s.get("insider_trades")]):
            lines.append("  （近30天无显著机构行为）")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    p.add_argument("--codes", help="逗号分隔，可用 code:name")
    args = p.parse_args()
    if args.codes:
        targets = {}
        for raw in args.codes.split(","):
            code, _, name = raw.strip().partition(":")
            if code:
                targets[code.zfill(6)] = name or code.zfill(6)
    else:
        targets = None
    data = collect_institution_data(targets)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(data))
