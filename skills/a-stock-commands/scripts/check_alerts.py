#!/usr/bin/env python3
"""价格提醒监控脚本。
读取 ~/.hermes/cron/output/alerts.json，检查所有活跃提醒是否触发价格条件。
如果触发，输出触发信号供 triage cron 消费。
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

ALERTS_FILE = os.path.expanduser("~/.hermes/cron/output/alerts.json")
TZ = timezone(timedelta(hours=8))

def load_alerts():
    if not os.path.exists(ALERTS_FILE):
        return []
    with open(ALERTS_FILE) as f:
        return json.load(f)

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
        triggered_flag = False
        
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
            # 需要历史数据对比，简化处理
            pass
    
    if updated:
        os.makedirs(os.path.dirname(ALERTS_FILE), exist_ok=True)
        with open(ALERTS_FILE, "w") as f:
            json.dump(alerts, f, ensure_ascii=False, indent=2)
    
    if triggered:
        print("=== 提醒触发 ===")
        for t in triggered:
            print(t)
        print(f"\n共 {len(triggered)} 条提醒触发。")
        # 触发后建议启动 triage
        print("SIGNAL: ALERT_TRIGGERED")
    else:
        # 没有触发时静默
        pass

if __name__ == "__main__":
    main()
