#!/usr/bin/env python3
"""
胜率统计 & 反馈闭环 — 信号历史命中率追踪
=========================================
记录所有 S/A/B 级推荐的历史表现，统计命中率。
数据文件: ~/.hermes/skills/stock-triage/data/signal_history.json

Usage:
  python3 performance_tracker.py                   # 查看统计
  python3 performance_tracker.py --record           # 记录当前信号（用于更新表现）
  python3 performance_tracker.py --json
"""

import json
import sys
import os
import urllib.request
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
from state_store import read_json, atomic_write_json

HISTORY_FILE = os.path.expanduser("~/.hermes/skills/stock-triage/data/signal_history.json")
os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)


def load_history() -> List[Dict]:
    return read_json(HISTORY_FILE, [])


def save_history(records: List[Dict]):
    atomic_write_json(HISTORY_FILE, records)


def fetch_price(code: str) -> Optional[float]:
    market = "sh" if code.startswith("6") else "sz"
    url = f"http://qt.gtimg.cn/q={market}{code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk")
        parts = raw.split("=")[1].strip().strip('"').split("~")
        return float(parts[3]) if parts[3] and len(parts) > 3 else None
    except Exception:
        return None


def record_signal(code: str, name: str, grade: str, score: float, price: float):
    """记录一个新的信号"""
    records = load_history()
    records.append({
        "code": code, "name": name, "grade": grade, "score": score,
        "signal_date": date.today().isoformat(),
        "signal_price": price,
        "current_price": None, "pnl_pct": None, "outcome": "pending",
    })
    save_history(records)
    return {"ok": True, "recorded": f"{name}({code}) {grade}级 @ {price}"}


def update_outcomes():
    """更新所有 pending 信号的当前表现"""
    records = load_history()
    updated = 0

    for r in records:
        if r.get("outcome") != "pending":
            continue

        price = fetch_price(r["code"])
        if price is None:
            continue

        r["current_price"] = price
        pnl_pct = round((price / r["signal_price"] - 1) * 100, 1)
        r["pnl_pct"] = pnl_pct

        days = (date.today() - date.fromisoformat(r["signal_date"])).days

        # 判断结果
        if pnl_pct >= 10:
            r["outcome"] = "win_big"
        elif pnl_pct >= 3:
            r["outcome"] = "win"
        elif pnl_pct <= -8:
            r["outcome"] = "loss_big"
        elif days > 30 and abs(pnl_pct) < 3:
            r["outcome"] = "neutral"
        elif days > 60:
            r["outcome"] = "loss" if pnl_pct < 0 else "win"

        if r["outcome"] != "pending":
            updated += 1

    if updated:
        save_history(records)

    return records


def compute_stats(records: List[Dict]) -> Dict:
    """计算统计数据"""
    closed = [r for r in records if r.get("outcome") and r["outcome"] != "pending"]
    if not closed:
        return {"total_signals": len(records), "closed": 0, "message": "尚无足够数据"}

    wins = [r for r in closed if r["outcome"].startswith("win")]
    losses = [r for r in closed if r["outcome"].startswith("loss")]

    by_grade = {}
    for g in ["S", "A", "B", "C"]:
        g_records = [r for r in records if r["grade"] == g]
        g_closed = [r for r in g_records if r.get("outcome") and r["outcome"] != "pending"]
        g_wins = [r for r in g_closed if r["outcome"].startswith("win")]
        by_grade[g] = {
            "total": len(g_records),
            "closed": len(g_closed),
            "win_rate": round(len(g_wins) / len(g_closed) * 100, 1) if g_closed else 0,
            "avg_pnl": round(sum(r.get("pnl_pct", 0) for r in g_closed) / len(g_closed), 1) if g_closed else 0,
        }

    return {
        "total_signals": len(records),
        "closed": len(closed),
        "pending": len(records) - len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1),
        "avg_pnl": round(sum(r.get("pnl_pct", 0) for r in closed) / len(closed), 1),
        "avg_hold_days": round(sum(
            (date.today() - date.fromisoformat(r["signal_date"])).days
            for r in closed
        ) / len(closed), 0),
        "by_grade": by_grade,
    }


def format_stats(stats: Dict, records: List[Dict]) -> str:
    lines = ["📈 **信号胜率统计**", f"⏰ {datetime.now().strftime('%Y-%m-%d')}", ""]

    if stats.get("closed", 0) == 0:
        lines.append("尚无已结算信号，继续积累数据...")
        lines.append(f"当前 {stats.get('pending', 0)} 个信号待结算")
        return "\n".join(lines)

    lines.append(f"📊 总信号: {stats['total_signals']} | 已结算: {stats['closed']} | "
                 f"待结算: {stats['pending']}")
    lines.append(f"🎯 胜率: **{stats['win_rate']}%** | 平均盈亏: **{stats['avg_pnl']:+.1f}%** | "
                 f"平均持仓: {stats['avg_hold_days']:.0f}天")
    lines.append("")

    # 分级统计
    lines.append("| 等级 | 信号数 | 已结算 | 胜率 | 平均盈亏 |")
    lines.append("|------|--------|--------|------|----------|")
    for g in ["S", "A", "B", "C"]:
        gs = stats["by_grade"].get(g, {})
        if gs.get("total", 0) > 0:
            lines.append(f"| {g} | {gs['total']} | {gs.get('closed',0)} | "
                         f"{gs.get('win_rate',0)}% | {gs.get('avg_pnl',0):+.1f}% |")

    # 最近信号
    recent = [r for r in records if r.get("outcome") and r["outcome"] != "pending"][-5:]
    if recent:
        lines.append("\n## 最近结算")
        for r in recent:
            emoji = "✅" if r["outcome"].startswith("win") else "❌"
            lines.append(f"  {emoji} {r['name']}({r['code']}) {r['grade']}级 "
                         f"@ {r['signal_price']} → {r['pnl_pct']:+.1f}%")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--record", nargs=4, metavar=("CODE","NAME","GRADE","PRICE"),
                   help="记录信号: code name grade price")
    p.add_argument("--score", type=float, default=5.0, help="评分")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    if args.record:
        code, name, grade, price = args.record
        result = record_signal(code, name, grade, args.score, float(price))
        print(json.dumps(result, ensure_ascii=False))

    else:
        records = update_outcomes()
        stats = compute_stats(records)
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        else:
            print(format_stats(stats, records))
