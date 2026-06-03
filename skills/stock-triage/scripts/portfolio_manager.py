#!/usr/bin/env python3
"""
持仓风控管理器 — 仓位跟踪 / 浮盈浮亏 / 止损止盈 / 组合相关性
============================================================
持仓文件: ~/.hermes/skills/stock-triage/data/portfolio.json

Usage:
  python3 portfolio_manager.py                  # 查看持仓
  python3 portfolio_manager.py --add 600011 华能国际 9.10 2000    # 新增持仓
  python3 portfolio_manager.py --update 600011 price 8.50         # 更新现价
  python3 portfolio_manager.py --close 600011 8.50                # 清仓
  python3 portfolio_manager.py --check                              # 风控检查
"""

import json
import sys
import os
import urllib.request
from datetime import datetime, date
from typing import Dict, List, Optional

# 共享状态存储
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
from state_store import read_json, atomic_write_json
from paths import data_file

PORTFOLIO_FILE = data_file("stock-triage", "portfolio.json")
HISTORY_FILE = data_file("stock-triage", "trade_history.json")
os.makedirs(os.path.dirname(PORTFOLIO_FILE), exist_ok=True)

# 风控参数
STOP_LOSS_PCT = -8.0      # 硬止损线
TAKE_PROFIT_PCT = 20.0    # 止盈线
TRAILING_STOP = 5.0       # 回撤止盈（从最高点回落5%）
MAX_SINGLE_POSITION = 25  # 单只最大仓位%
MAX_SECTOR_EXPOSURE = 40  # 单板块最大敞口%
PORTFOLIO_SIZE = 100000   # 默认总资金（用户应修改）


def load_portfolio() -> Dict:
    return read_json(PORTFOLIO_FILE, {"cash": PORTFOLIO_SIZE, "positions": [], "total_cost": 0})


def save_portfolio(data: Dict):
    atomic_write_json(PORTFOLIO_FILE, data)


def load_history() -> List:
    return read_json(HISTORY_FILE, [])


def save_history(record: Dict):
    history = load_history()
    history.append(record)
    atomic_write_json(HISTORY_FILE, history)


def fetch_price(code: str) -> Optional[Dict]:
    """获取实时价格"""
    market = "sh" if code.startswith("6") else "sz"
    url = f"http://qt.gtimg.cn/q={market}{code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk")
        parts = raw.split("=")[1].strip().strip('"').split("~")
        if len(parts) < 40:
            return None
        return {
            "price": float(parts[3]) if parts[3] else None,
            "change_pct": float(parts[32]) if parts[32] else None,
            "name": parts[1] if len(parts) > 1 else code,
        }
    except Exception:
        return None


def add_position(code: str, name: str, cost: float, shares: int):
    """新增持仓"""
    pf = load_portfolio()
    total_cost = cost * shares

    # 检查是否已持有
    for pos in pf["positions"]:
        if pos["code"] == code:
            # 加仓：加权平均成本
            old_total = pos["cost"] * pos["shares"]
            new_total = old_total + total_cost
            pos["shares"] += shares
            pos["cost"] = round(new_total / pos["shares"], 2)
            pos["add_date"] = date.today().isoformat()
            pf["total_cost"] += total_cost
            save_portfolio(pf)
            return {"ok": True, "action": "加仓", "code": code, "name": name,
                    "cost": pos["cost"], "shares": pos["shares"]}

    pf["positions"].append({
        "code": code, "name": name, "cost": cost, "shares": shares,
        "buy_date": date.today().isoformat(), "add_date": date.today().isoformat(),
        "peak_price": cost  # 用于回撤止盈
    })
    pf["total_cost"] += total_cost
    save_portfolio(pf)
    return {"ok": True, "action": "开仓", "code": code, "name": name, "cost": cost, "shares": shares}


def close_position(code: str, sell_price: float):
    """清仓"""
    pf = load_portfolio()
    for i, pos in enumerate(pf["positions"]):
        if pos["code"] == code:
            proceeds = sell_price * pos["shares"]
            cost_basis = pos["cost"] * pos["shares"]
            pnl = proceeds - cost_basis
            pnl_pct = (sell_price / pos["cost"] - 1) * 100

            record = {
                "code": code, "name": pos["name"],
                "buy_date": pos["buy_date"], "sell_date": date.today().isoformat(),
                "cost": pos["cost"], "sell_price": sell_price, "shares": pos["shares"],
                "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 1),
                "hold_days": (date.today() - date.fromisoformat(pos["buy_date"])).days
            }
            save_history(record)

            pf["total_cost"] -= cost_basis
            pf["positions"].pop(i)
            save_portfolio(pf)
            return {"ok": True, "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 1),
                    "record": record}
    return {"error": f"未找到持仓: {code}"}


def update_prices(pf: Dict) -> Dict:
    """更新所有持仓的现价"""
    alerts = []
    total_value = 0

    for pos in pf["positions"]:
        data = fetch_price(pos["code"])
        if data and data.get("price"):
            pos["current_price"] = data["price"]
            pos["change_pct"] = data.get("change_pct")
            pos["market_value"] = data["price"] * pos["shares"]
            pos["pnl"] = round((data["price"] - pos["cost"]) * pos["shares"], 2)
            pos["pnl_pct"] = round((data["price"] / pos["cost"] - 1) * 100, 1)

            # 更新峰值（用于回撤止盈）
            if data["price"] > pos.get("peak_price", 0):
                pos["peak_price"] = data["price"]

            total_value += pos["market_value"]

            # 止损检查
            if pos["pnl_pct"] <= STOP_LOSS_PCT:
                alerts.append({
                    "level": "🔴 止损",
                    "msg": f"{pos['name']}({pos['code']}) 浮亏{pos['pnl_pct']}%，触发硬止损！成本{pos['cost']}，现价{data['price']}"
                })

            # 回撤止盈
            peak = pos.get("peak_price", pos["cost"])
            drawdown_from_peak = (data["price"] / peak - 1) * 100 if peak > pos["cost"] else 0
            if drawdown_from_peak <= -TRAILING_STOP and pos["pnl_pct"] > 0:
                alerts.append({
                    "level": "🟡 止盈",
                    "msg": f"{pos['name']}({pos['code']}) 从高点{peak}回落{abs(drawdown_from_peak):.1f}%，触发回撤止盈"
                })
        else:
            pos["current_price"] = None
            pos["change_pct"] = None

    # 仓位集中度检查
    if total_value > 0:
        for pos in pf["positions"]:
            weight = pos.get("market_value", 0) / total_value * 100
            pos["weight_pct"] = round(weight, 1)
            if weight > MAX_SINGLE_POSITION:
                alerts.append({
                    "level": "🟡 风控",
                    "msg": f"{pos['name']} 仓位{weight:.0f}%，超过单只上限{MAX_SINGLE_POSITION}%"
                })

    return {"alerts": alerts, "total_value": round(total_value, 2)}


def format_check_report(pf: Dict, result: Dict) -> str:
    """风控报告"""
    lines = [
        "📊 **持仓风控报告**",
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    total_value = result.get("total_value", 0)
    total_pnl = sum(p.get("pnl", 0) for p in pf["positions"])
    total_pnl_pct = round(total_pnl / pf["total_cost"] * 100, 1) if pf["total_cost"] else 0

    lines.append(f"💰 总市值: **{total_value:,.0f}** | 总成本: {pf['total_cost']:,.0f}")
    lines.append(f"📈 总盈亏: **{total_pnl:+,.0f}** ({total_pnl_pct:+.1f}%)")
    lines.append("")

    if not pf["positions"]:
        lines.append("（当前无持仓）")
        return "\n".join(lines)

    lines.append("| 标的 | 成本 | 现价 | 盈亏% | 仓位% | 状态 |")
    lines.append("|------|------|------|-------|-------|------|")
    for pos in pf["positions"]:
        price = pos.get("current_price", "?")
        pnl = pos.get("pnl_pct", 0)
        weight = pos.get("weight_pct", 0)
        emoji = "🟢" if (pnl or 0) > 0 else "🔴"
        lines.append(f"| {pos['name']} | {pos['cost']} | {price} | {pnl:+.1f}% | {weight:.0f}% | {emoji} |")

    alerts = result.get("alerts", [])
    if alerts:
        lines.append(f"\n## ⚠️ {len(alerts)}条风控警报")
        for a in alerts:
            lines.append(f"- {a['level']}: {a['msg']}")
    else:
        lines.append("\n✅ 无风控警报，持仓正常")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="持仓风控管理器")
    p.add_argument("--add", nargs=3, metavar=("CODE", "NAME", "COST"), help="新增持仓")
    p.add_argument("--shares", type=int, default=1000, help="股数（默认1000）")
    p.add_argument("--close", nargs=2, metavar=("CODE", "PRICE"), help="清仓")
    p.add_argument("--check", action="store_true", help="风控检查")
    p.add_argument("--json", action="store_true", help="JSON输出")
    args = p.parse_args()

    if args.add:
        code, name, cost = args.add
        result = add_position(code, name, float(cost), args.shares)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.close:
        code, price = args.close
        result = close_position(code, float(price))
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.check or True:  # 默认检查
        pf = load_portfolio()
        result = update_prices(pf)
        save_portfolio(pf)
        if args.json:
            print(json.dumps({"portfolio": pf, "result": result}, ensure_ascii=False, indent=2, default=str))
        else:
            print(format_check_report(pf, result))
