#!/usr/bin/env python3
"""价格提醒监控脚本。
读取 $HERMES_HOME/cron/output/alerts.json，检查所有活跃提醒是否触发价格条件。
如果触发，输出触发信号供 triage cron 消费。
"""
import os
from datetime import datetime, timezone, timedelta

_COMMON_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "common")
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from paths import cron_output_dir
from http_client import request_text
from state_store import mutate_json, read_json

ALERTS_FILE = os.path.join(cron_output_dir(), "alerts.json")
TZ = timezone(timedelta(hours=8))


def load_alerts():
    data = read_json(ALERTS_FILE)
    return data if isinstance(data, list) else []


def get_price(code: str) -> float:
    """获取个股实时价格（腾讯 API）"""
    prefix = "sh" if code.startswith("6") else "sz"
    url = f"http://qt.gtimg.cn/q={prefix}{code}"
    try:
        raw = request_text(
            url,
            source="tencent",
            timeout=10,
            encoding="gbk",
            headers={"User-Agent": "Mozilla/5.0"},
        ).data
        parts = raw.split("=")[1].strip().strip('"').split("~")
        return float(parts[3])
    except Exception:
        return 0.0

def apply_alert_triggers(alerts, prices):
    triggered = []
    for alert in alerts:
        if not alert.get("active", True):
            continue

        price = prices.get(alert.get("code"), 0.0)
        if price == 0.0:
            continue

        alert_type = alert.get("type", "")
        target = alert.get("price", 0)

        if alert_type == "stop_loss" and price <= target:
            triggered.append(f"🛑 {alert['name']}({alert['code']}) 止损触发: {price} ≤ {target}")
            alert["active"] = False
            alert["triggered_at"] = datetime.now(TZ).isoformat()
            alert["trigger_price"] = price
        elif alert_type == "breakout" and price >= target:
            triggered.append(f"🚀 {alert['name']}({alert['code']}) 突破触发: {price} ≥ {target}")
            alert["active"] = False
            alert["triggered_at"] = datetime.now(TZ).isoformat()
            alert["trigger_price"] = price

    return triggered


def main():
    snapshot = load_alerts()
    if not snapshot:
        return

    codes = {item.get("code") for item in snapshot if item.get("active", True) and item.get("code")}
    prices = {code: get_price(code) for code in codes}
    triggered = []

    def _mutate(current):
        nonlocal triggered
        alerts = current if isinstance(current, list) else []
        triggered = apply_alert_triggers(alerts, prices)
        return alerts

    mutate_json(ALERTS_FILE, _mutate, [])

    if triggered:
        print("=== 提醒触发 ===")
        for t in triggered:
            print(t)
        print(f"\n共 {len(triggered)} 条提醒触发。")
        print("SIGNAL: ALERT_TRIGGERED")

if __name__ == "__main__":
    main()
