#!/usr/bin/env python3
"""
价格提醒监控脚本。
读取 ~/.hermes/cron/output/alerts.json，检查所有活跃提醒是否触发价格条件。
如果触发，输出触发信号供 triage cron 消费。

所有 JSON 读写走 state_store.atomic_write_json / read_json，确保并发安全。
"""
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

# 添加 common 模块到路径
_COMMON_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                           "skills", "common")
if _COMMON_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_COMMON_DIR))

from state_store import read_json, atomic_write_json

ALERTS_FILE = os.path.expanduser("~/.hermes/cron/output/alerts.json")
TZ = timezone(timedelta(hours=8))


def load_alerts():
    data = read_json(ALERTS_FILE)
    return data if isinstance(data, list) else []


def get_price(code: str) -> float:
    """获取个股实时价格（腾讯 API）"""
    prefix = "sh" if code.startswith("6") else "sz"
    url = f"http://qt.gtimg.cn/q={prefix}{code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk")
        parts = raw.split("=")[1].strip().strip('"').split("~")
        return float(parts[3])
    except Exception:
        return 0.0


def main():
    alerts = load_alerts()
    if not alerts:
        return

    triggered = []
    updated = False

    for alert in alerts:
        if not alert.get("active", True):
            continue

        price = get_price(alert["code"])
        if price == 0.0:
            continue

        alert_type = alert.get("type", "")
        target = alert.get("price", 0)

        if alert_type == "stop_loss" and price <= target:
            triggered.append(f"🛑 {alert['name']}({alert['code']}) 止损触发: {price} ≤ {target}")
            alert["active"] = False
            alert["triggered_at"] = datetime.now(TZ).isoformat()
            alert["trigger_price"] = price
            updated = True
        elif alert_type == "breakout" and price >= target:
            triggered.append(f"🚀 {alert['name']}({alert['code']}) 突破触发: {price} ≥ {target}")
            alert["active"] = False
            alert["triggered_at"] = datetime.now(TZ).isoformat()
            alert["trigger_price"] = price
            updated = True
        elif alert_type == "volatility":
            pass

    if updated:
        atomic_write_json(ALERTS_FILE, alerts)

    if triggered:
        print("=== 提醒触发 ===")
        for t in triggered:
            print(t)
        print(f"\n共 {len(triggered)} 条提醒触发。")
        print("SIGNAL: ALERT_TRIGGERED")


if __name__ == "__main__":
    main()
