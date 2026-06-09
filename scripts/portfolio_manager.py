#!/usr/bin/env python3
"""
持仓风控管理器 — 仓位跟踪 / 资金流水 / 交易历史 / 止损止盈
==========================================================
持仓文件: $HERMES_HOME/skills/stock-triage/data/portfolio.json
历史文件: $HERMES_HOME/skills/stock-triage/data/trade_history.json
资金流水: $HERMES_HOME/skills/stock-triage/data/cash_flow.json

Usage:
  python3 portfolio_manager.py                              # 查看持仓+可用资金
  python3 portfolio_manager.py --add 600011 华能国际 9.10 2000    # 开仓(自动扣现金)
  python3 portfolio_manager.py --close 600011 8.50                # 清仓(自动加回现金)
  python3 portfolio_manager.py --deposit 50000                    # 存入资金
  python3 portfolio_manager.py --withdraw 10000                   # 取出资金
  python3 portfolio_manager.py --history                          # 交易历史
  python3 portfolio_manager.py --balance                          # 资金快照
  python3 portfolio_manager.py --check                            # 风控检查
"""

import json
import sys
import os
import urllib.request
from datetime import datetime, date
from typing import Dict, List, Optional

# 共享状态存储
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
from state_store import read_json, atomic_write_json, update_json_list, mutate_json
from paths import data_file

PORTFOLIO_FILE = data_file("stock-triage", "portfolio.json")
HISTORY_FILE = data_file("stock-triage", "trade_history.json")
CASHFLOW_FILE = data_file("stock-triage", "cash_flow.json")
os.makedirs(os.path.dirname(PORTFOLIO_FILE), exist_ok=True)

# 风控参数
STOP_LOSS_PCT = -8.0      # 硬止损线
TAKE_PROFIT_PCT = 20.0    # 止盈线
TRAILING_STOP = 5.0       # 回撤止盈（从最高点回落5%）
MAX_SINGLE_POSITION = 25  # 单只最大仓位%
MAX_SECTOR_EXPOSURE = 40  # 单板块最大敞口%
PORTFOLIO_SIZE = 100000   # 默认总资金（用户应修改）


def _default_portfolio() -> Dict:
    return {"cash": PORTFOLIO_SIZE, "positions": [], "total_cost": 0, "cash_reconciled": True}


def _normalize(pf: Optional[Dict]) -> Dict:
    """补全缺失字段，并对历史文件做一次性现金对账。

    v1.0 的 add_position 从不扣减 cash，老文件里的 cash 仍是「总本金」而非
    「可用现金」。首次被新版加载时，按 可用现金 = 本金 - 当前持仓成本 重算一次，
    并打上 cash_reconciled 标记，避免重复对账。新文件自带标记，无副作用。
    """
    pf = dict(pf) if isinstance(pf, dict) else {}
    pf.setdefault("positions", [])
    pf.setdefault("total_cost", 0)
    pf.setdefault("cash", PORTFOLIO_SIZE)
    if not pf.get("cash_reconciled"):
        pf["cash"] = round(pf["cash"] - pf.get("total_cost", 0), 2)
        pf["cash_reconciled"] = True
    return pf


def load_portfolio() -> Dict:
    """只读加载（含归一化，但不持久化迁移结果）。"""
    return _normalize(read_json(PORTFOLIO_FILE, _default_portfolio()))


def ensure_portfolio() -> Dict:
    """加载并在锁内完成一次性迁移 + 持久化。返回归一化后的持仓。"""
    return mutate_json(PORTFOLIO_FILE, _normalize, default=_default_portfolio())


def save_portfolio(data: Dict):
    atomic_write_json(PORTFOLIO_FILE, data)


def load_history() -> List:
    return read_json(HISTORY_FILE, [])


def load_cashflow() -> List:
    return read_json(CASHFLOW_FILE, [])


# ======================== 资金管理 ========================

def record_cash_flow(action: str, amount: float, note: str = "") -> Dict:
    """记录资金流水（并发安全追加）。返回流水记录。"""
    record = {
        "action": action,
        "amount": round(amount, 2),
        "note": note,
        "timestamp": datetime.now().isoformat(),
    }
    update_json_list(CASHFLOW_FILE, record)
    return record


def deposit(amount: float) -> Dict:
    """入金（事务式改现金 + 记流水）。"""
    if amount <= 0:
        return {"error": f"入金金额必须为正: {amount}"}

    def _mut(pf):
        pf = _normalize(pf)
        pf["cash"] = round(pf["cash"] + amount, 2)
        return pf

    pf = mutate_json(PORTFOLIO_FILE, _mut, default=_default_portfolio())
    record_cash_flow("deposit", amount, f"存入 {amount:,.0f}")
    return {"ok": True, "action": "deposit", "amount": round(amount, 2), "cash": pf["cash"]}


def withdraw(amount: float) -> Dict:
    """出金（事务式校验余额 + 改现金 + 记流水）。余额不足拒绝。"""
    if amount <= 0:
        return {"error": f"出金金额必须为正: {amount}"}
    outcome: Dict = {}

    def _mut(pf):
        pf = _normalize(pf)
        if pf["cash"] < amount:
            outcome["error"] = f"资金不足: 可用{pf['cash']:,.0f}，要取{amount:,.0f}"
            return pf  # 不变更
        pf["cash"] = round(pf["cash"] - amount, 2)
        outcome["ok"] = True
        outcome["cash"] = pf["cash"]
        return pf

    mutate_json(PORTFOLIO_FILE, _mut, default=_default_portfolio())
    if outcome.get("ok"):
        record_cash_flow("withdraw", amount, f"取出 {amount:,.0f}")
        return {"ok": True, "action": "withdraw", "amount": round(amount, 2), "cash": outcome["cash"]}
    return {"error": outcome["error"]}


# ======================== 交易操作 ========================

def add_position(code: str, name: str, cost: float, shares: int) -> Dict:
    """开仓/加仓（事务式：校验现金 + 扣现金 + 加权平均成本，全程单锁）。"""
    if cost <= 0 or shares <= 0:
        return {"error": f"价格与股数必须为正: cost={cost}, shares={shares}"}
    total_cost = cost * shares
    outcome: Dict = {}

    def _mut(pf):
        pf = _normalize(pf)
        if pf["cash"] < total_cost:
            outcome["error"] = f"可用资金不足: 需要{total_cost:,.0f}，可用{pf['cash']:,.0f}"
            return pf  # 不变更

        pos_found = next((p for p in pf["positions"] if p["code"] == code), None)
        if pos_found:
            # 加仓：加权平均成本
            old_total = pos_found["cost"] * pos_found["shares"]
            pos_found["shares"] += shares
            pos_found["cost"] = round((old_total + total_cost) / pos_found["shares"], 2)
            pos_found["add_date"] = date.today().isoformat()
            action = "加仓"
        else:
            pf["positions"].append({
                "code": code, "name": name, "cost": cost, "shares": shares,
                "buy_date": date.today().isoformat(), "add_date": date.today().isoformat(),
                "peak_price": cost,
            })
            pos_found = pf["positions"][-1]
            action = "开仓"

        pf["total_cost"] = round(pf["total_cost"] + total_cost, 2)
        pf["cash"] = round(pf["cash"] - total_cost, 2)
        outcome.update(ok=True, action=action, cost=pos_found["cost"],
                       shares=pos_found["shares"], cash_remaining=pf["cash"])
        return pf

    mutate_json(PORTFOLIO_FILE, _mut, default=_default_portfolio())
    if not outcome.get("ok"):
        return {"error": outcome["error"]}
    # 持仓已落盘后再追加流水（崩溃至多丢一条日志，不会出现幽灵流水）
    record_cash_flow("buy", total_cost, f"{outcome['action']}: {name}({code}) {shares}股 @ {cost}")
    return {"ok": True, "code": code, "name": name, **outcome}


def close_position(code: str, sell_price: float) -> Dict:
    """清仓（事务式：加回现金 + 移除持仓，全程单锁；落盘后再记历史/流水）。"""
    if sell_price <= 0:
        return {"error": f"卖出价必须为正: {sell_price}"}
    outcome: Dict = {}

    def _mut(pf):
        pf = _normalize(pf)
        idx = next((i for i, p in enumerate(pf["positions"]) if p["code"] == code), None)
        if idx is None:
            outcome["error"] = f"未找到持仓: {code}"
            return pf

        pos = pf["positions"][idx]
        proceeds = sell_price * pos["shares"]
        cost_basis = pos["cost"] * pos["shares"]
        pnl = proceeds - cost_basis
        pnl_pct = (sell_price / pos["cost"] - 1) * 100

        hold_days = 0
        try:
            hold_days = (date.today() - date.fromisoformat(pos["buy_date"])).days
        except Exception:
            pass

        outcome["record"] = {
            "code": code, "name": pos["name"],
            "buy_date": pos["buy_date"], "sell_date": date.today().isoformat(),
            "cost": pos["cost"], "sell_price": sell_price, "shares": pos["shares"],
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 1),
            "hold_days": hold_days,
        }
        pf["total_cost"] = max(round(pf["total_cost"] - cost_basis, 2), 0)
        pf["cash"] = round(pf["cash"] + proceeds, 2)
        pf["positions"].pop(idx)
        outcome.update(ok=True, pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 1),
                       hold_days=hold_days, proceeds=proceeds, cash_remaining=pf["cash"])
        return pf

    mutate_json(PORTFOLIO_FILE, _mut, default=_default_portfolio())
    if not outcome.get("ok"):
        return {"error": outcome["error"]}
    rec = outcome["record"]
    update_json_list(HISTORY_FILE, rec)
    record_cash_flow("sell", outcome["proceeds"], f"清仓: {rec['name']}({code}) 盈亏{rec['pnl_pct']:+.1f}%")
    return {"ok": True, "pnl": outcome["pnl"], "pnl_pct": outcome["pnl_pct"],
            "hold_days": outcome["hold_days"], "cash_remaining": outcome["cash_remaining"],
            "record": rec}


# ======================== 行情更新与风控 ========================

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


def _apply_prices(pf: Dict, fetched: Dict[str, Optional[Dict]]) -> Dict:
    """把预取到的现价合并进持仓并算风控告警。fetched: code -> 行情或 None。"""
    alerts = []
    total_value = 0

    for pos in pf["positions"]:
        data = fetched.get(pos["code"])
        if data and data.get("price"):
            pos["current_price"] = data["price"]
            pos["change_pct"] = data.get("change_pct")
            pos["market_value"] = data["price"] * pos["shares"]
            pos["pnl"] = round((data["price"] - pos["cost"]) * pos["shares"], 2)
            pos["pnl_pct"] = round((data["price"] / pos["cost"] - 1) * 100, 1)

            if data["price"] > pos.get("peak_price", 0):
                pos["peak_price"] = data["price"]

            total_value += pos["market_value"]

            if pos["pnl_pct"] <= STOP_LOSS_PCT:
                alerts.append({
                    "level": "🔴 止损",
                    "msg": f"{pos['name']}({pos['code']}) 浮亏{pos['pnl_pct']}%，触发硬止损！成本{pos['cost']}，现价{data['price']}"
                })

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

    # 仓位集中度
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


def refresh_prices() -> tuple:
    """拉取现价（锁外网络IO）→ 在事务内合并刷价。返回 (持仓, 风控结果)。

    现价请求放在锁外，避免持锁期间阻塞在 HTTP；合并写回放在 mutate 事务内，
    期间被并发开/清仓改动的持仓表不会被这次刷价覆盖丢失。
    """
    snapshot = ensure_portfolio()
    fetched = {pos["code"]: fetch_price(pos["code"]) for pos in snapshot["positions"]}
    result: Dict = {}

    def _mut(pf):
        pf = _normalize(pf)
        result.update(_apply_prices(pf, fetched))
        return pf

    pf = mutate_json(PORTFOLIO_FILE, _mut, default=_default_portfolio())
    return pf, result


# ======================== 格式化输出 ========================

def format_check_report(pf: Dict, result: Dict) -> str:
    """风控报告"""
    lines = [
        "📊 **持仓风控报告**",
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    cash = pf.get("cash", 0)
    total_value = result.get("total_value", 0)
    total_pnl = sum(p.get("pnl", 0) for p in pf["positions"])
    total_cost = pf.get("total_cost", 0)

    lines.append(f"💵 **可用资金:** {cash:,.0f}")
    lines.append(f"📦 持仓市值: {total_value:,.0f} | 持仓成本: {total_cost:,.0f}")
    if total_cost > 0:
        pnl_pct = round(total_pnl / total_cost * 100, 1)
        lines.append(f"📈 浮动盈亏: **{total_pnl:+,.0f}** ({pnl_pct:+.1f}%)")
    lines.append(f"💰 **总资产: {cash + total_value:,.0f}** (现金+市值)")
    lines.append("")

    if not pf["positions"]:
        lines.append("（当前无持仓）")
        return "\n".join(lines)

    lines.append("| 标的 | 成本 | 现价 | 盈亏% | 市值 | 仓位% | 状态 |")
    lines.append("|------|------|------|-------|------|-------|------|")
    for pos in pf["positions"]:
        price = pos.get("current_price", "?")
        pnl = pos.get("pnl_pct", 0) or 0
        mv = pos.get("market_value", 0) or 0
        weight = pos.get("weight_pct", 0) or 0
        emoji = "🟢" if pnl > 0 else "🔴"
        lines.append(
            f"| {pos['name']} | {pos['cost']} | {price} | {pnl:+.1f}% | {mv:,.0f} | {weight:.0f}% | {emoji} |"
        )

    alerts = result.get("alerts", [])
    if alerts:
        lines.append(f"\n## ⚠️ {len(alerts)}条风控警报")
        for a in alerts:
            lines.append(f"- {a['level']}: {a['msg']}")
    else:
        lines.append("\n✅ 无风控警报，持仓正常")

    return "\n".join(lines)


def format_history(records: List[Dict]) -> str:
    """交易历史"""
    if not records:
        return "📋 暂无交易历史"

    lines = [
        "📋 **交易历史**",
        f"共 {len(records)} 笔已清仓交易",
        "",
        "| 日期 | 标的 | 成本→卖出 | 盈亏 | 持仓天数 |",
        "|------|------|-----------|------|----------|",
    ]
    recent = list(reversed(records[-20:]))  # 仅展示最近20条
    if len(records) > len(recent):
        lines[1] = f"共 {len(records)} 笔已清仓交易（下表仅显示最近 {len(recent)} 笔）"
    for r in recent:
        lines.append(
            f"| {r.get('sell_date', '?')} | {r['name']}({r['code']}) | "
            f"{r['cost']}→{r['sell_price']} | "
            f"{r.get('pnl_pct', 0):+.1f}% ({r.get('pnl', 0):+,.0f}) | "
            f"{r.get('hold_days', '?')}天 |"
        )

    lines.append("")
    # 累计盈亏/胜率均按【全量】记录统计，与上表展示子集解耦
    total_pnl = sum(r.get("pnl", 0) for r in records)
    win_count = sum(1 for r in records if r.get("pnl", 0) > 0)
    total = len(records)
    win_rate = round(win_count / total * 100, 1) if total else 0
    lines.append(f"📈 累计盈亏: **{total_pnl:+,.0f}** | 胜率: **{win_rate}%** ({win_count}/{total})")
    return "\n".join(lines)


def format_balance(pf: Dict) -> str:
    """资金快照"""
    cash = pf.get("cash", 0)
    total_cost = pf.get("total_cost", 0)
    lines = [
        "💰 **资金快照**",
        "",
        f"💵 可用现金: **{cash:,.2f}**",
        f"📦 持仓成本: {total_cost:,.2f}",
    ]
    if pf["positions"]:
        total_mv = sum(p.get("market_value", p["cost"] * p["shares"]) for p in pf["positions"])
        lines.append(f"📊 持仓市值: {total_mv:,.0f}")
        lines.append(f"💰 总资产: **{cash + total_mv:,.2f}**")
    else:
        lines.append(f"💰 总资产: **{cash:,.2f}** (全部现金)")
    return "\n".join(lines)


# ======================== CLI ========================

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="持仓风控管理器")
    p.add_argument("--add", nargs=3, metavar=("CODE", "NAME", "COST"), help="开仓/加仓")
    p.add_argument("--shares", type=int, default=1000, help="股数（默认1000）")
    p.add_argument("--close", nargs=2, metavar=("CODE", "PRICE"), help="清仓")
    p.add_argument("--deposit", type=float, metavar="AMOUNT", help="存入资金")
    p.add_argument("--withdraw", type=float, metavar="AMOUNT", help="取出资金")
    p.add_argument("--check", action="store_true", help="风控检查")
    p.add_argument("--history", action="store_true", help="查看交易历史")
    p.add_argument("--balance", action="store_true", help="资金快照")
    p.add_argument("--json", action="store_true", help="JSON输出")
    args = p.parse_args()

    if args.deposit is not None:
        result = deposit(args.deposit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    elif args.withdraw is not None:
        result = withdraw(args.withdraw)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    elif args.add:
        code, name, cost = args.add
        result = add_position(code, name, float(cost), args.shares)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif result.get("error"):
            print(f"❌ {result['error']}")
            sys.exit(1)
        else:
            print(f"✅ {result.get('action', '')}: "
                  f"{result.get('name', '')}({result.get('code', '')}) "
                  f"{result.get('shares', 0):,}股 @ {result.get('cost', 0)} | "
                  f"剩余现金: {result.get('cash_remaining', 0):,.0f}")

    elif args.close:
        code, price = args.close
        result = close_position(code, float(price))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if result.get("ok"):
                print(f"✅ 清仓: {result['record']['name']}({code}) | "
                      f"盈亏: {result['pnl_pct']:+.1f}% ({result['pnl']:+,.0f}) | "
                      f"持仓{result['hold_days']}天 | 剩余现金: {result['cash_remaining']:,.0f}")
            else:
                print(f"❌ {result.get('error', '未知错误')}")
                sys.exit(1)

    elif args.history:
        records = load_history()
        print(format_history(records))

    elif args.balance:
        pf = ensure_portfolio()
        print(format_balance(pf))

    elif args.check:
        pf, result = refresh_prices()
        if args.json:
            print(json.dumps({"portfolio": pf, "result": result},
                             ensure_ascii=False, indent=2, default=str))
        else:
            print(format_check_report(pf, result))

    else:
        # 默认：持仓+资金快照
        pf, result = refresh_prices()
        print(format_balance(pf))
        print("")
        print(format_check_report(pf, result))
