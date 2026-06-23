#!/usr/bin/env python3
"""
持仓风控管理器 — 仓位跟踪 / 资金流水 / 交易历史 / 止损止盈
==========================================================
持仓文件: $HERMES_HOME/skills/stock-triage/data/portfolio.json
历史文件: $HERMES_HOME/skills/stock-triage/data/trade_history.json
资金流水: $HERMES_HOME/skills/stock-triage/data/cash_flow.json

Usage:
  python3 portfolio_manager.py                              # 查看持仓+可用资金
  python3 portfolio_manager.py --add 600519 贵州茅台 150.00 100  # 开仓(自动扣现金)
  python3 portfolio_manager.py --close 600519 155.00              # 清仓(自动加回现金)
  python3 portfolio_manager.py --deposit 50000                    # 存入资金
  python3 portfolio_manager.py --withdraw 10000                   # 取出资金
  python3 portfolio_manager.py --history                          # 交易历史
  python3 portfolio_manager.py --balance                          # 资金快照
  python3 portfolio_manager.py --check                            # 风控检查
"""

import json
import sys
import os
from datetime import datetime, date
from typing import Dict, List, Optional

# 共享状态存储
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
from state_store import read_json, atomic_write_json, update_json_list, mutate_json
from paths import data_file
from a_share_rules import t1_constraint
from data_access_config import risk_settings
from data_provider import fetch_tencent_quote
from http_client import DataSourceError
import monitor_registry
import signal_ledger

PORTFOLIO_FILE = data_file("stock-triage", "portfolio.json")
HISTORY_FILE = data_file("stock-triage", "trade_history.json")
CASHFLOW_FILE = data_file("stock-triage", "cash_flow.json")
LEDGER_FILE = signal_ledger.LEDGER_FILE
os.makedirs(os.path.dirname(PORTFOLIO_FILE), exist_ok=True)

# 风控参数：统一配置缺失时由 data_access_config 回退到历史默认值。
_RISK_CONFIG = risk_settings()
STOP_LOSS_PCT = float(_RISK_CONFIG["stop_loss_pct"])
TAKE_PROFIT_PCT = float(_RISK_CONFIG["take_profit_pct"])
TRAILING_STOP = float(_RISK_CONFIG["trailing_stop_pct"])
MAX_SINGLE_POSITION = float(_RISK_CONFIG["max_single_position_pct"])
MAX_SECTOR_EXPOSURE = float(_RISK_CONFIG["max_sector_exposure_pct"])


def _default_portfolio() -> Dict:
    return {
        "cash": 0.0,
        "positions": [],
        "total_cost": 0,
        "cash_reconciled": True,
        "account_state": "unconfigured",
    }


def _normalize(pf: Optional[Dict]) -> Dict:
    """补全缺失字段，并对历史文件做一次性现金对账。

    v1.0 的 add_position 从不扣减 cash，老文件里的 cash 仍是「总本金」而非
    「可用现金」。首次被新版加载时，按 可用现金 = 本金 - 当前持仓成本 重算一次，
    并打上 cash_reconciled 标记，避免重复对账。新文件自带标记，无副作用。
    """
    pf = dict(pf) if isinstance(pf, dict) else {}
    pf.setdefault("positions", [])
    pf.setdefault("total_cost", 0)
    pf.setdefault("cash", 0.0)
    for pos in pf["positions"]:
        if not isinstance(pos.get("lots"), list) or not pos["lots"]:
            known_dates = [
                str(value)
                for value in (pos.get("buy_date"), pos.get("add_date"))
                if value
            ]
            pos["lots"] = [{
                "shares": int(pos.get("shares") or 0),
                "cost": float(pos.get("cost") or 0),
                # Legacy aggregate positions cannot reconstruct individual lots.
                # Use the latest known acquisition date to fail closed on T+1.
                "acquired_on": max(known_dates) if known_dates else "1970-01-01",
            }]
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


def _latest_stock_links(code: str) -> Dict:
    """Reuse the newest recommendation/trade correlation for this stock."""
    try:
        events = signal_ledger.read_events(LEDGER_FILE)
    except (OSError, TimeoutError):
        events = []
    for event in reversed(events):
        payload = event.get("payload") or {}
        if str(payload.get("code") or "").zfill(6) != str(code).zfill(6):
            continue
        links = dict(event.get("links") or {})
        if links.get("correlation_id"):
            return links
    return signal_ledger.make_links(
        correlation_id=None,
        signal_id=f"position:{str(code).zfill(6)}",
        monitor_id=f"stock:{str(code).zfill(6)}",
    )


def _record_trade_execution(
    *,
    code: str,
    name: str,
    side: str,
    price: float,
    shares: int,
    trade_date: str,
    action: str,
    pnl: float | None = None,
    pnl_pct: float | None = None,
) -> Dict:
    base_links = _latest_stock_links(code)
    trade_id = signal_ledger.make_trade_execution_id(
        str(code).zfill(6),
        side,
        trade_date,
        f"{price:.4f}",
        str(shares),
        action,
    )
    links = {**base_links, "trade_id": trade_id, "monitor_id": f"stock:{str(code).zfill(6)}"}
    return signal_ledger.append_event(
        "trade.executed",
        links,
        {
            "code": str(code).zfill(6),
            "name": name,
            "side": side,
            "action": action,
            "price": price,
            "shares": shares,
            "trade_date": trade_date,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "status": "executed",
        },
        idempotency_key=f"trade.executed:{trade_id}",
        ledger_file=LEDGER_FILE,
    ) or {}


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


def reconcile_cash(amount: float, *, source: str, asof: str | None = None) -> Dict:
    """Replace runtime cash with a verified balance and append an audit flow."""
    if amount < 0:
        return {"error": f"现金余额不能为负: {amount}"}
    if not str(source or "").strip():
        return {"error": "余额来源不能为空"}
    previous = {"cash": 0.0}

    def _mut(pf):
        pf = _normalize(pf)
        previous["cash"] = float(pf.get("cash") or 0)
        pf["cash"] = round(float(amount), 2)
        pf["cash_source"] = str(source).strip()
        pf["cash_asof"] = asof or date.today().isoformat()
        pf["cash_reconciled"] = True
        pf["account_state"] = "verified"
        return pf

    portfolio = mutate_json(PORTFOLIO_FILE, _mut, default=_default_portfolio())
    delta = round(portfolio["cash"] - previous["cash"], 2)
    record_cash_flow(
        "reconcile_cash",
        delta,
        f"余额校准为 {portfolio['cash']:,.2f}，来源={portfolio['cash_source']}，时点={portfolio['cash_asof']}",
    )
    return {
        "ok": True,
        "action": "reconcile_cash",
        "cash": portfolio["cash"],
        "previous_cash": round(previous["cash"], 2),
        "delta": delta,
        "source": portfolio["cash_source"],
        "asof": portfolio["cash_asof"],
    }


# ======================== 交易操作 ========================

def add_position(
    code: str,
    name: str,
    cost: float,
    shares: int,
    trade_date: str | None = None,
) -> Dict:
    """开仓/加仓（事务式：校验现金 + 扣现金 + 加权平均成本，全程单锁）。"""
    if cost <= 0 or shares <= 0:
        return {"error": f"价格与股数必须为正: cost={cost}, shares={shares}"}
    total_cost = cost * shares
    acquired_on = trade_date or date.today().isoformat()
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
            pos_found["add_date"] = acquired_on
            pos_found.setdefault("lots", []).append({
                "shares": shares,
                "cost": cost,
                "acquired_on": acquired_on,
            })
            action = "加仓"
        else:
            pf["positions"].append({
                "code": code, "name": name, "cost": cost, "shares": shares,
                "buy_date": acquired_on, "add_date": acquired_on,
                "peak_price": cost,
                "lots": [{
                    "shares": shares,
                    "cost": cost,
                    "acquired_on": acquired_on,
                }],
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
    trade_event = _record_trade_execution(
        code=code,
        name=name,
        side="buy",
        price=cost,
        shares=shares,
        trade_date=acquired_on,
        action="add" if outcome["action"] == "加仓" else "open",
    )
    monitor_registry.activate(
        "stock",
        code,
        name,
        source="portfolio_buy",
        force=True,
        metadata={
            "position_linked": True,
            **(trade_event.get("links") or _latest_stock_links(code)),
        },
    )
    return {"ok": True, "code": code, "name": name, **outcome}


def close_position(
    code: str,
    sell_price: float,
    trade_date: str | None = None,
) -> Dict:
    """清仓（事务式：加回现金 + 移除持仓，全程单锁；落盘后再记历史/流水）。"""
    if sell_price <= 0:
        return {"error": f"卖出价必须为正: {sell_price}"}
    current_date = trade_date or date.today().isoformat()
    outcome: Dict = {}

    def _mut(pf):
        pf = _normalize(pf)
        idx = next((i for i, p in enumerate(pf["positions"]) if p["code"] == code), None)
        if idx is None:
            outcome["error"] = f"未找到持仓: {code}"
            return pf

        pos = pf["positions"][idx]
        locked_lots = []
        for lot in pos.get("lots", []):
            constraint = t1_constraint(lot.get("acquired_on"), current_date)
            if not constraint["sell_allowed"]:
                locked_lots.append({**lot, "constraint": constraint})
        if locked_lots:
            earliest = max(
                lot["constraint"]["earliest_sell_date"]
                for lot in locked_lots
            )
            outcome.update({
                "error": f"A股T+1限制：{code}含当日买入/加仓股份，最早{earliest}可全部卖出",
                "code": "T1_LOCKED",
                "earliest_sell_date": earliest,
                "locked_shares": sum(int(lot.get("shares") or 0) for lot in locked_lots),
            })
            return pf
        proceeds = sell_price * pos["shares"]
        cost_basis = pos["cost"] * pos["shares"]
        pnl = proceeds - cost_basis
        pnl_pct = (sell_price / pos["cost"] - 1) * 100

        hold_days = 0
        try:
            hold_days = (date.fromisoformat(current_date) - date.fromisoformat(pos["buy_date"])).days
        except Exception:
            pass

        outcome["record"] = {
            "code": code, "name": pos["name"],
            "buy_date": pos["buy_date"], "sell_date": current_date,
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
        return {
            key: value
            for key, value in outcome.items()
            if key in {"error", "code", "earliest_sell_date", "locked_shares"}
        }
    rec = outcome["record"]
    update_json_list(HISTORY_FILE, rec)
    record_cash_flow("sell", outcome["proceeds"], f"清仓: {rec['name']}({code}) 盈亏{rec['pnl_pct']:+.1f}%")
    trade_event = _record_trade_execution(
        code=code,
        name=rec["name"],
        side="sell",
        price=sell_price,
        shares=rec["shares"],
        trade_date=current_date,
        action="close",
        pnl=rec["pnl"],
        pnl_pct=rec["pnl_pct"],
    )
    monitor_registry.cancel(
        "stock",
        code,
        reason="position_closed",
        manual=False,
        status="closed",
        metadata=trade_event.get("links") or _latest_stock_links(code),
    )
    return {"ok": True, "pnl": outcome["pnl"], "pnl_pct": outcome["pnl_pct"],
            "hold_days": outcome["hold_days"], "cash_remaining": outcome["cash_remaining"],
            "record": rec}


# ======================== 行情更新与风控 ========================

def fetch_price(code: str) -> Optional[Dict]:
    """获取实时价格"""
    try:
        quote = fetch_tencent_quote(code)
        return {
            "price": quote.get("price"),
            "change_pct": quote.get("change_pct"),
            "name": quote.get("name") or code,
            "fetched_at": quote.get("fetched_at"),
        }
    except DataSourceError:
        return None


def _position_t1_state(pos: Dict, asof: str | None = None) -> Dict:
    current = asof or date.today().isoformat()
    lots = pos.get("lots") or [{
        "shares": pos.get("shares"),
        "acquired_on": pos.get("buy_date") or "1970-01-01",
    }]
    locked = []
    sellable = 0
    for lot in lots:
        constraint = t1_constraint(lot.get("acquired_on"), current)
        shares = int(lot.get("shares") or 0)
        if constraint["sell_allowed"]:
            sellable += shares
        else:
            locked.append({**lot, "constraint": constraint})
    return {
        "sellable_shares": sellable,
        "locked_shares": sum(int(item.get("shares") or 0) for item in locked),
        "earliest_sell_date": (
            max(item["constraint"]["earliest_sell_date"] for item in locked)
            if locked else None
        ),
    }


def _apply_prices(pf: Dict, fetched: Dict[str, Optional[Dict]]) -> Dict:
    """把预取到的现价合并进持仓并算风控告警。fetched: code -> 行情或 None。"""
    alerts = []
    total_value = 0

    for pos in pf["positions"]:
        t1_state = _position_t1_state(pos)
        pos.update(t1_state)
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
                if t1_state["locked_shares"]:
                    message = (
                        f"{pos['name']}({pos['code']}) 浮亏{pos['pnl_pct']}%，风险已触发；"
                        f"{t1_state['locked_shares']}股受A股T+1锁定，最早"
                        f"{t1_state['earliest_sell_date']}处置"
                    )
                else:
                    message = (
                        f"{pos['name']}({pos['code']}) 浮亏{pos['pnl_pct']}%，触发硬止损！"
                        f"成本{pos['cost']}，现价{data['price']}"
                    )
                alerts.append({
                    "level": "🔴 止损",
                    "msg": message,
                    "execution_status": "t1_locked" if t1_state["locked_shares"] else "sellable",
                    **t1_state,
                })

            peak = pos.get("peak_price", pos["cost"])
            drawdown_from_peak = (data["price"] / peak - 1) * 100 if peak > pos["cost"] else 0
            if drawdown_from_peak <= -TRAILING_STOP and pos["pnl_pct"] > 0:
                if t1_state["locked_shares"]:
                    trail_message = (
                        f"{pos['name']}({pos['code']}) 从高点{peak}回落"
                        f"{abs(drawdown_from_peak):.1f}%，止盈条件已触发；"
                        f"T+1锁定股份最早{t1_state['earliest_sell_date']}处置"
                    )
                else:
                    trail_message = (
                        f"{pos['name']}({pos['code']}) 从高点{peak}回落"
                        f"{abs(drawdown_from_peak):.1f}%，触发回撤止盈"
                    )
                alerts.append({
                    "level": "🟡 止盈",
                    "msg": trail_message,
                    "execution_status": "t1_locked" if t1_state["locked_shares"] else "sellable",
                    **t1_state,
                })
        else:
            pos["current_price"] = None
            pos["change_pct"] = None

    # 仓位集中度（按总资产 = 现金 + 持仓市值）
    if total_value > 0:
        total_asset = pf.get("cash", 0) + total_value
        for pos in pf["positions"]:
            weight = pos.get("market_value", 0) / total_asset * 100
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
    p.add_argument("--reconcile-cash", type=float, metavar="AMOUNT", help="用已核验账户余额校准现金")
    p.add_argument("--cash-source", default="user_confirmed", help="余额来源标识")
    p.add_argument("--cash-asof", help="余额核验日期 YYYY-MM-DD")
    p.add_argument("--check", action="store_true", help="风控检查")
    p.add_argument("--history", action="store_true", help="查看交易历史")
    p.add_argument("--balance", action="store_true", help="资金快照")
    p.add_argument("--json", action="store_true", help="JSON输出")
    args = p.parse_args()

    if args.reconcile_cash is not None:
        result = reconcile_cash(
            args.reconcile_cash,
            source=args.cash_source,
            asof=args.cash_asof,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result.get("ok") else 1)

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
        if args.json:
            print(json.dumps({"schema": "portfolio_balance_v1", **pf}, ensure_ascii=False, indent=2))
        else:
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
