#!/usr/bin/env python3
"""
事件日历 — 财报日/解禁日/分红/政策窗口
=====================================
数据源：东方财富数据中心

Usage:
  python3 event_calendar.py
  python3 event_calendar.py --json
"""

import json
import sys
import os
import urllib.request
from datetime import datetime, date, timedelta
from typing import Dict, Any, List

TRACKED = {
    "600011": "华能国际", "002156": "通富微电", "600584": "长电科技",
    "002185": "华天科技", "000021": "深科技", "600667": "太极实业",
}

# 固定政策窗口（每年）
POLICY_WINDOWS = [
    ("2026-03-04", "2026-03-11", "全国两会 — 关注新质生产力/科技/消费政策"),
    ("2026-10-01", "2026-10-07", "国庆黄金周 — 关注消费数据"),
    ("2026-07-15", "2026-07-20", "三中全会 (待定) — 重大改革政策窗口"),
    ("2026-12-01", "2026-12-15", "中央经济工作会议 — 来年政策定调"),
    ("2026-04-30", "2026-04-30", "年报披露截止日"),
    ("2026-08-31", "2026-08-31", "半年报披露截止日"),
    ("2026-10-31", "2026-10-31", "三季报披露截止日"),
]


def fetch_eastmoney_api(url: str) -> Dict:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def fetch_lockup_expiry(code: str) -> List[Dict]:
    """限售解禁"""
    market = "SH" if code.startswith("6") else "SZ"
    url = (f"https://datacenter.eastmoney.com/securities/api/data/v1/get?"
           f"reportName=RPT_STOCK_LOCKUP&columns=ALL&"
           f"filter=(SECUCODE=%22{code}.{market}%22)&"
           f"pageSize=5&pageNumber=1&sortColumns=UNLOCKDATE&sortTypes=1")
    data = fetch_eastmoney_api(url)
    results = []
    if data.get("result") and data["result"].get("data"):
        for item in data["result"]["data"][:5]:
            unlock_date = item.get("UNLOCKDATE", "")[:10]
            if unlock_date >= date.today().isoformat():
                results.append({
                    "date": unlock_date,
                    "shares_yi": round(float(item.get("UNLOCKNUM", 0)) / 10000, 1),
                    "ratio": item.get("UNLOCKRATIO", 0),
                })
    return results


def fetch_dividend(code: str) -> Dict:
    """分红信息"""
    market = "SH" if code.startswith("6") else "SZ"
    url = (f"https://datacenter.eastmoney.com/securities/api/data/v1/get?"
           f"reportName=RPT_SHAREBONUS_DET&columns=ALL&"
           f"filter=(SECUCODE=%22{code}.{market}%22)&"
           f"pageSize=1&pageNumber=1&sortColumns=EXRIGHTDATE&sortTypes=-1")
    data = fetch_eastmoney_api(url)
    if data.get("result") and data["result"].get("data"):
        item = data["result"]["data"][0]
        return {
            "bonus": item.get("BONUSIT_RATIO", 0),
            "ex_date": item.get("EXRIGHTDATE", "")[:10],
            "reg_date": item.get("REGISTRATIONDATE", "")[:10],
        }
    return {}


def get_upcoming_policy_windows(days_ahead: int = 30) -> List[str]:
    """未来N天的政策窗口"""
    today = date.today()
    cutoff = today + timedelta(days=days_ahead)
    upcoming = []
    for start, end, desc in POLICY_WINDOWS:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
        if today <= e and s <= cutoff:
            upcoming.append(f"{start}~{end}: {desc}")
    return upcoming


def collect_events() -> Dict:
    result = {
        "timestamp": datetime.now().isoformat(),
        "today": date.today().isoformat(),
        "stocks": [],
        "policy_windows": get_upcoming_policy_windows(30),
        "alerts": [],
    }

    for code, name in TRACKED.items():
        stock = {"code": code, "name": name, "lockups": [], "dividend": {}}

        # 解禁
        lockups = fetch_lockup_expiry(code)
        for lu in lockups:
            if lu["ratio"] and float(lu["ratio"]) > 5:
                result["alerts"].append({
                    "level": "🟡",
                    "msg": f"⚠️ {name} 将于{lu['date']}解禁{lu['shares_yi']}万股（占{lu['ratio']}%），注意抛压"
                })
        stock["lockups"] = lockups

        # 分红
        div = fetch_dividend(code)
        if div and div.get("ex_date"):
            stock["dividend"] = div
            if div.get("bonus", 0) > 3:
                result["alerts"].append({
                    "level": "ℹ️",
                    "msg": f"{name} 拟10派{div['bonus']}元，除权日{div['ex_date']}"
                })

        result["stocks"].append(stock)

    return result


def format_report(data: Dict) -> str:
    lines = ["📅 **事件日历**", f"⏰ {data['today']}", ""]

    # 政策窗口
    pw = data.get("policy_windows", [])
    if pw:
        lines.append("## 🏛️ 近期政策窗口")
        for w in pw[:5]:
            lines.append(f"- {w}")
        lines.append("")

    # 个股事件
    lines.append("## 📊 跟踪标的近期事件")
    for s in data.get("stocks", []):
        events = []
        if s.get("lockups"):
            events.append(f"解禁: {s['lockups'][0]['date']}({s['lockups'][0].get('ratio','')}%)")
        if s.get("dividend") and s["dividend"].get("bonus"):
            events.append(f"分红: 10派{s['dividend']['bonus']}元({s['dividend'].get('ex_date','')})")

        if events:
            lines.append(f"- {s['name']}: {' | '.join(events)}")
        else:
            lines.append(f"- {s['name']}: 近期无重大事件")

    alerts = data.get("alerts", [])
    if alerts:
        lines.append(f"\n## ⚡ {len(alerts)}条事件提醒")
        for a in alerts:
            lines.append(f"- {a['level']} {a['msg']}")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    data = collect_events()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(data))
