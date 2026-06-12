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
from datetime import datetime, time as dtime
from typing import Dict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
from paths import data_file
from state_store import atomic_write_json, read_json
from data_access_config import intraday_settings
from data_provider import fetch_tencent_quote
from http_client import DataSourceError
import monitor_registry

# 兼容测试/显式注入；生产默认观察集由持仓和 monitor_registry 动态生成。
TRACKED_CODES = []
TRACKED_NAMES = {}

ALERT_CACHE = data_file("stock-triage", "intraday_alerts.json")
PORTFOLIO_FILE = data_file("stock-triage", "portfolio.json")

_MONITOR_CONFIG = intraday_settings()
LIMIT_MOVE_PCT = float(_MONITOR_CONFIG["limit_move_pct"])
HIGH_TURNOVER_PCT = float(_MONITOR_CONFIG["high_turnover_pct"])
SURGE_PCT = float(_MONITOR_CONFIG["surge_pct"])
DIRECTIONAL_MOVE_PCT = float(_MONITOR_CONFIG["directional_move_pct"])


def in_trading_session(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    current = now.time()
    return (
        dtime(9, 30) <= current <= dtime(11, 30)
        or dtime(13, 0) <= current <= dtime(15, 0)
    )


def fetch_realtime(code: str) -> Dict:
    try:
        quote = fetch_tencent_quote(code)
        return {
            key: quote.get(key)
            for key in (
                "price",
                "change_pct",
                "high",
                "low",
                "volume",
                "amount",
                "turnover",
                "prev_close",
                "fetched_at",
            )
        }
    except DataSourceError:
        return {}


def load_alert_cache() -> Dict:
    return read_json(ALERT_CACHE, {})


def save_alert_cache(cache: Dict):
    atomic_write_json(ALERT_CACHE, cache)


def tracked_universe() -> Dict[str, str]:
    tracked = {
        str(code).zfill(6): TRACKED_NAMES.get(str(code), str(code))
        for code in TRACKED_CODES
    }
    portfolio = read_json(PORTFOLIO_FILE, {})
    positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else []
    monitor_registry.sync_positions(positions)
    for position in positions:
        code = str(position.get("code") or "").zfill(6)
        if code:
            tracked[code] = str(position.get("name") or code)
    tracked.update(monitor_registry.active_stock_map())
    return tracked


def check_intraday() -> Dict:
    """检测盘中异动（阈值触发）"""
    alerts = []
    cache = load_alert_cache()
    now = datetime.now()
    now_str = now.strftime("%H:%M")
    today = now.strftime("%Y%m%d")
    if cache.get("_date", "") != today:
        cache = {"_date": today}

    universe = tracked_universe()
    for code, name in universe.items():
        data = fetch_realtime(code)
        if not data.get("price"):
            continue

        price = data["price"]
        pct = data.get("change_pct", 0)
        turnover = data.get("turnover", 0)

        # 1. 涨跌停检测
        if pct and pct >= LIMIT_MOVE_PCT:
            key = f"zt_{code}"
            if key not in cache:
                alerts.append({"level": "🔴", "type": "涨停",
                               "msg": f"{name}({code}) 涨停！现价{price} (+{pct}%)"})
                cache[key] = now_str
        elif pct and pct <= -LIMIT_MOVE_PCT:
            key = f"dt_{code}"
            if key not in cache:
                alerts.append({"level": "🔴", "type": "跌停",
                               "msg": f"{name}({code}) 跌停！现价{price} ({pct}%)"})
                cache[key] = now_str

        # 2. 放量检测（换手率>10%且之前未报）
        if turnover > HIGH_TURNOVER_PCT:
            key = f"vol_{code}"
            if key not in cache:
                direction = (
                    "拉升"
                    if pct > DIRECTIONAL_MOVE_PCT
                    else ("砸盘" if pct < -DIRECTIONAL_MOVE_PCT else "异动")
                )
                alerts.append({"level": "🟡", "type": "放量",
                               "msg": f"{name} 换手{turnover:.1f}%{direction}，成交{data.get('amount',0)/1e8:.1f}亿"})
                cache[key] = now_str

        # 3. 急涨急跌（5%以上）
        if abs(pct) >= SURGE_PCT:
            key = f"surge_{code}"
            if key not in cache:
                direction = "急涨" if pct > 0 else "急跌"
                alerts.append({"level": "🟡", "type": direction,
                               "msg": f"{name} {direction}{abs(pct):.1f}%，现价{price}"})
                cache[key] = now_str

    save_alert_cache(cache)

    return {
        "timestamp": now.isoformat(),
        "time": now_str,
        "tracked_count": len(universe),
        "tracked_stocks": universe,
        "alerts": alerts,
        "has_alerts": len(alerts) > 0,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()

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
