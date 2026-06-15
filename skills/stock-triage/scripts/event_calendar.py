#!/usr/bin/env python3
"""事件日历 — 限售解禁/分红除权/政策窗口
=====================================
数据源：东方财富数据中心 (datacenter.eastmoney.com)

Usage:
  python3 event_calendar.py                          # 默认跟踪标的
  python3 event_calendar.py --json                   # JSON 输出
  python3 event_calendar.py --portfolio              # 从 portfolio.json 读取持仓
  python3 event_calendar.py --codes 603859,600011    # 指定代码列表

"""

import json
import os
import sys
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

COMMON_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "common"))
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)

from eastmoney_intelligence import fetch_dividend as _fetch_dividend
from eastmoney_intelligence import fetch_lockups
from http_client import DataSourceError
from paths import data_file

# 默认跟踪标的（不持有时也会关注，用于空仓期监控）
DEFAULT_TRACKED = {
    "600011": "华能国际", "002156": "通富微电", "600584": "长电科技",
    "002185": "华天科技", "000021": "深科技", "600667": "太极实业",
}


def load_portfolio_codes() -> Optional[Dict[str, str]]:
    """从 portfolio.json 读取持仓标的"""
    pf_path = data_file("stock-triage", "portfolio.json")
    if not os.path.exists(pf_path):
        return None
    try:
        with open(pf_path) as f:
            pf = json.load(f)
    except Exception:
        return None
    codes = {}
    for pos in pf.get("positions", []):
        codes[pos["code"]] = pos.get("name", pos["code"])
    return codes if codes else None


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


def fetch_dividend(code: str) -> Optional[Dict]:
    """分红除权信息 — RPT_SHAREBONUS_DET（字段已更新为 2026 版）"""
    try:
        return _fetch_dividend(code, asof=date.today())
    except DataSourceError:
        return None


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


def collect_events(codes: Optional[Dict[str, str]] = None) -> Dict:
    """采集事件。codes=None 时使用默认跟踪列表。"""
    if codes is None:
        codes = DEFAULT_TRACKED
    if not codes:
        return {
            "timestamp": datetime.now().isoformat(),
            "today": date.today().isoformat(),
            "stocks": [],
            "policy_windows": [],
            "alerts": [],
        }

    result = {
        "timestamp": datetime.now().isoformat(),
        "today": date.today().isoformat(),
        "stocks": [],
        "policy_windows": get_upcoming_policy_windows(30),
        "alerts": [],
    }

    for code, name in codes.items():
        stock = {"code": code, "name": name, "dividend": None, "lockups": []}

        try:
            lockups = fetch_lockups(code, asof=date.today(), forward_days=90)
            stock["lockups"] = lockups.get("upcoming") or []
            for lockup in stock["lockups"]:
                ratio = float(lockup.get("ratio_pct") or 0)
                level = "🔴" if ratio >= 10 else "🟡" if ratio >= 3 else "ℹ️"
                result["alerts"].append({
                    "level": level,
                    "msg": (
                        f"{name} {lockup.get('date')}解禁"
                        f"{ratio:.2f}%（{lockup.get('type') or '类型未知'}）"
                    ),
                })
        except DataSourceError as exc:
            stock["lockup_error"] = exc.to_dict()

        # 分红除权
        div = fetch_dividend(code)
        if div:
            stock["dividend"] = div
            if div.get("is_upcoming") and div.get("bonus_per_10", 0) > 0:
                result["alerts"].append({
                    "level": "ℹ️",
                    "msg": (f"{name} 拟10派{div['bonus_per_10']}元，"
                            f"除权日{div['ex_date']}，登记日{div['reg_date']}")
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
    lines.append("## 📊 持仓标的近期事件")
    has_any = False
    for s in data.get("stocks", []):
        events = []
        div = s.get("dividend")
        if div:
            prefix = "🔜" if div.get("is_upcoming") else "📋"
            events.append(
                f"{prefix} 分红: 10派{div['bonus_per_10']}元"
                f"（除权{div['ex_date']}，{div['progress']}）"
            )
        if s.get("lockups"):
            lu = s["lockups"][0]
            events.append(
                f"⚠️ 解禁: {lu['date']}({float(lu.get('ratio_pct') or 0):.2f}%)"
            )
        elif s.get("lockup_error"):
            events.append("解禁数据暂不可用")

        if events:
            lines.append(f"- **{s['name']}**: {' | '.join(events)}")
            has_any = True
        else:
            lines.append(f"- {s['name']}: 近期无重大事件")

    if not has_any:
        lines.append("\n✅ 无近期分红/解禁事件，安心持有。")

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
    p.add_argument("--portfolio", action="store_true")
    p.add_argument("--codes", type=str, default="")
    args = p.parse_args()

    if args.codes:
        codes = {}
        for c in args.codes.split(","):
            c = c.strip()
            if c:
                codes[c] = c
    elif args.portfolio:
        codes = load_portfolio_codes()
        if not codes:
            print("⚠️ portfolio.json 无持仓数据")
            sys.exit(0)
    else:
        codes = None

    data = collect_events(codes)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(data))
