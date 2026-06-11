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
import os
import urllib.request
import urllib.parse
from datetime import datetime
from typing import Dict, List

TRACKED_CODES = {
    "600011": "华能国际", "002156": "通富微电", "600584": "长电科技",
    "002185": "华天科技", "000021": "深科技", "600667": "太极实业",
}


def fetch_eastmoney_api(url: str) -> Dict:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def fetch_research_visits(code: str) -> List[Dict]:
    """机构调研（近30天）"""
    market = "SH" if code.startswith("6") else "SZ"
    # 东财机构调研接口
    url = (f"https://datacenter.eastmoney.com/securities/api/data/v1/get?"
           f"reportName=RPT_ORG_SURVEY&columns=ALL&"
           f"filter=(SECUCODE=%22{code}.{market}%22)&"
           f"pageSize=5&pageNumber=1&sortColumns=NOTICEDATE&sortTypes=-1")
    data = fetch_eastmoney_api(url)
    results = []
    if data.get("result") and data["result"].get("data"):
        for item in data["result"]["data"][:5]:
            results.append({
                "date": item.get("NOTICEDATE", "")[:10],
                "org_count": item.get("RECEPTIONAMOUNT", 0),
                "summary": (item.get("MAINPOINT", "") or "")[:80],
            })
    return results


def fetch_analyst_reports(code: str) -> List[Dict]:
    """券商研报（近90天）"""
    market = "SH" if code.startswith("6") else "SZ"
    url = (f"https://datacenter.eastmoney.com/securities/api/data/v1/get?"
           f"reportName=RPT_RESREPORT_SEARCH&columns=ALL&"
           f"filter=(SECUCODE=%22{code}.{market}%22)&"
           f"pageSize=5&pageNumber=1&sortColumns=NOTICEDATE&sortTypes=-1")
    data = fetch_eastmoney_api(url)
    results = []
    if data.get("result") and data["result"].get("data"):
        for item in data["result"]["data"][:5]:
            results.append({
                "date": item.get("NOTICEDATE", "")[:10],
                "org": item.get("SNAME", ""),
                "rating": item.get("RATING", ""),
                "target_price": item.get("TARGETPRICE", ""),
            })
    return results


def fetch_insider_trades(code: str) -> List[Dict]:
    """大股东增减持"""
    market = "SH" if code.startswith("6") else "SZ"
    url = (f"https://datacenter.eastmoney.com/securities/api/data/v1/get?"
           f"reportName=RPT_HOLDER_TRADE_STOCK&columns=ALL&"
           f"filter=(SECUCODE=%22{code}.{market}%22)&"
           f"pageSize=5&pageNumber=1&sortColumns=NOTICEDATE&sortTypes=-1")
    data = fetch_eastmoney_api(url)
    results = []
    if data.get("result") and data["result"].get("data"):
        for item in data["result"]["data"][:5]:
            results.append({
                "date": item.get("NOTICEDATE", "")[:10],
                "name": item.get("PARTICIPANTNAME", ""),
                "direction": "增持" if item.get("TRADETYPE", "") == "1" else "减持",
                "shares": item.get("TRADENUM", 0),
            })
    return results


def fetch_serpapi_inst_news(code: str, name: str) -> List[Dict]:
    """通过 SerpAPI 搜索机构相关新闻"""
    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        return []
    query = f"{name} {code} 券商研报 机构调研 评级 目标价"
    url = f"https://serpapi.com/search?engine=google_news&q={urllib.parse.quote(query)}&num=3&api_key={api_key}&gl=cn&hl=zh-cn"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        results = []
        for item in data.get("news_results", [])[:3]:
            results.append({
                "title": item.get("title", ""),
                "source": item.get("source", {}).get("name", ""),
                "date": item.get("date", ""),
            })
        return results
    except Exception:
        return []


def collect_institution_data() -> Dict:
    result = {"timestamp": datetime.now().isoformat(), "stocks": [], "alerts": []}

    for code, name in TRACKED_CODES.items():
        stock = {"code": code, "name": name, "research_visits": [], "analyst_reports": [],
                 "insider_trades": [], "news": []}

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
        stock["news"] = fetch_serpapi_inst_news(code, name)

        result["stocks"].append(stock)

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
    args = p.parse_args()
    data = collect_institution_data()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(data))
