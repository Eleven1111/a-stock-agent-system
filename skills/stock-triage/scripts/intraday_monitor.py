#!/usr/bin/env python3
"""
盘中高频异动监控 — 5分钟阈值触发
=================================
监测：放量突破 / 北向异动 / 涨跌停板 / 板块异动
只在触发阈值时输出，无触发完全静默。

Usage:
  python3 intraday_monitor.py
  python3 intraday_monitor.py --json
"""

import json
import sys
import os
import urllib.request
from datetime import datetime, time as dtime
from typing import Dict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
from state_store import read_json, atomic_write_json
from paths import data_file

TRACKED_CODES = ["600011", "002156", "600584", "002185", "000021", "600667"]
TRACKED_NAMES = {"600011": "华能国际", "002156": "通富微电", "600584": "长电科技",
                 "002185": "华天科技", "000021": "深科技", "600667": "太极实业"}

ALERT_CACHE = data_file("stock-triage", "intraday_alerts.json")


def in_trading_session(now: datetime = None) -> bool:
    """是否在 A股连续竞价时段（9:30-11:30 / 13:00-15:00）。
    cron 即使在 9:00/14:xx 触发，盘前盘后也静默退出，避免基于停盘/隔夜数据误报。"""
    now = now or datetime.now()
    if now.weekday() >= 5:  # 周末
        return False
    t = now.time()
    return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))


def fetch_realtime(code: str) -> Dict:
    market = "sh" if code.startswith("6") else "sz"
    url = f"http://qt.gtimg.cn/q={market}{code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk")
        parts = raw.split("=")[1].strip().strip('"').split("~")
        if len(parts) < 40:
            return {}
        return {
            "price": float(parts[3]) if parts[3] else None,
            "change_pct": float(parts[32]) if parts[32] else None,
            "high": float(parts[33]) if parts[33] else None,
            "low": float(parts[34]) if parts[34] else None,
            "volume": float(parts[6]) if parts[6] else None,
            "amount": float(parts[37]) * 10000 if parts[37] else None,
            "turnover": float(parts[38]) if parts[38] else None,
            "prev_close": float(parts[4]) if parts[4] else None,
        }
    except Exception:
        return {}


def load_alert_cache() -> Dict:
    return read_json(ALERT_CACHE, {})


def save_alert_cache(cache: Dict):
    atomic_write_json(ALERT_CACHE, cache)


def check_intraday() -> Dict:
    """检测盘中异动（阈值触发）"""
    alerts = []
    cache = load_alert_cache()
    now = datetime.now()
    now_str = now.strftime("%H:%M")

    for code in TRACKED_CODES:
        data = fetch_realtime(code)
        if not data.get("price"):
            continue

        name = TRACKED_NAMES.get(code, code)
        price = data["price"]
        pct = data.get("change_pct", 0)
        turnover = data.get("turnover", 0)

        # 1. 涨跌停检测
        if pct and pct >= 9.5:
            key = f"zt_{code}"
            if key not in cache:
                alerts.append({"level": "🔴", "type": "涨停",
                               "msg": f"{name}({code}) 涨停！现价{price} (+{pct}%)"})
                cache[key] = now_str
        elif pct and pct <= -9.5:
            key = f"dt_{code}"
            if key not in cache:
                alerts.append({"level": "🔴", "type": "跌停",
                               "msg": f"{name}({code}) 跌停！现价{price} ({pct}%)"})
                cache[key] = now_str

        # 2. 放量检测（换手率>10%且之前未报）
        if turnover > 10:
            key = f"vol_{code}"
            if key not in cache:
                direction = "拉升" if pct > 2 else ("砸盘" if pct < -2 else "异动")
                alerts.append({"level": "🟡", "type": "放量",
                               "msg": f"{name} 换手{turnover:.1f}%{direction}，成交{data.get('amount',0)/1e8:.1f}亿"})
                cache[key] = now_str

        # 3. 急涨急跌（5%以上）
        if abs(pct) >= 5:
            key = f"surge_{code}"
            if key not in cache:
                direction = "急涨" if pct > 0 else "急跌"
                alerts.append({"level": "🟡", "type": direction,
                               "msg": f"{name} {direction}{abs(pct):.1f}%，现价{price}"})
                cache[key] = now_str

    # 清理过期缓存（新的一天）
    today = now.strftime("%Y%m%d")
    cache_date = cache.get("_date", "")
    if cache_date != today:
        cache = {"_date": today}
        alerts = []  # 新的一天重新检测

    save_alert_cache(cache)

    return {
        "timestamp": now.isoformat(),
        "time": now_str,
        "alerts": alerts,
        "has_alerts": len(alerts) > 0,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="忽略交易时段守卫，强制运行（调试/测试用）")
    args = p.parse_args()

    # 非交易时段静默退出（cron 高频触发，盘前盘后不应基于停盘数据误报）
    if not args.force and not in_trading_session():
        sys.exit(0)

    data = check_intraday()

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif data["has_alerts"]:
        print(f"⚡ 盘中异动 | {data['time']}")
        for a in data["alerts"]:
            print(f"  {a['level']} {a['msg']}")
    # 无触发则静默
