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
from functools import wraps
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Mapping, Optional

# 共享状态存储
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
from state_store import file_lock, read_json, atomic_write_json, update_json_list, mutate_json
from paths import data_file
from a_share_rules import t1_constraint, is_trading_day
from data_access_config import risk_settings
from data_provider import fetch_tencent_quote
from http_client import DataSourceError
from portfolio_policy import evaluate_new_position, portfolio_value
import daban_config
import monitor_registry
import event_projection
import signal_ledger

PORTFOLIO_FILE = data_file("stock-triage", "portfolio.json")
HISTORY_FILE = data_file("stock-triage", "trade_history.json")
CASHFLOW_FILE = data_file("stock-triage", "cash_flow.json")
LEDGER_FILE = signal_ledger.LEDGER_FILE
PROJECTION_CHECKPOINT_FILE = data_file(
    "stock-triage", "portfolio_projection_checkpoint.json"
)
_DEFAULT_PORTFOLIO_FILE = PORTFOLIO_FILE
_DEFAULT_PROJECTION_CHECKPOINT_FILE = PROJECTION_CHECKPOINT_FILE
os.makedirs(os.path.dirname(PORTFOLIO_FILE), exist_ok=True)

# 风控参数：统一配置缺失时由 data_access_config 回退到历史默认值。
_RISK_CONFIG = risk_settings()
STOP_LOSS_PCT = float(_RISK_CONFIG["stop_loss_pct"])
TAKE_PROFIT_PCT = float(_RISK_CONFIG["take_profit_pct"])
TRAILING_STOP = float(_RISK_CONFIG["trailing_stop_pct"])
MAX_SINGLE_POSITION = float(_RISK_CONFIG["max_single_position_pct"])
MAX_SECTOR_EXPOSURE = float(_RISK_CONFIG["max_sector_exposure_pct"])
# 打板仓位专属时间止损：非固定百分比，参数走 daban_thresholds 单一事实源。
POSITION_TIME_STOP_DAYS = int(daban_config.section("market_gate")["position_time_stop_trading_days"])


def _projection_checkpoint_file() -> str:
    if (
        PORTFOLIO_FILE != _DEFAULT_PORTFOLIO_FILE
        and PROJECTION_CHECKPOINT_FILE == _DEFAULT_PROJECTION_CHECKPOINT_FILE
    ):
        return f"{PORTFOLIO_FILE}.checkpoint.json"
    return PROJECTION_CHECKPOINT_FILE


def _event_transaction(function):
    @wraps(function)
    def _serialized(*args, **kwargs):
        with file_lock(f"{PORTFOLIO_FILE}.event-transaction", timeout=30):
            return function(*args, **kwargs)

    return _serialized


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
    recovery = _recover_portfolio_projection()
    if recovery.get("status") != "ok":
        raise RuntimeError("portfolio projection requires replay")
    return _normalize(read_json(PORTFOLIO_FILE, _default_portfolio()))


def ensure_portfolio() -> Dict:
    """加载并在锁内完成一次性迁移 + 持久化。返回归一化后的持仓。"""
    recovery = _recover_portfolio_projection()
    if recovery.get("status") != "ok":
        raise RuntimeError("portfolio projection requires replay")
    return mutate_json(PORTFOLIO_FILE, _normalize, default=_default_portfolio())


def save_portfolio(data: Dict):
    atomic_write_json(PORTFOLIO_FILE, data)


def load_history() -> List:
    return read_json(HISTORY_FILE, [])


def load_cashflow() -> List:
    return read_json(CASHFLOW_FILE, [])


def _append_unique_projection_record(path: str, record: Mapping[str, Any]) -> None:
    identity = record.get("event_id")

    def _mutate(records: Any) -> list[dict[str, Any]]:
        items = [dict(item) for item in records] if isinstance(records, list) else []
        if identity and any(item.get("event_id") == identity for item in items):
            return items
        items.append(dict(record))
        return items

    mutate_json(path, _mutate, default=[])


def _project_portfolio_event(event: Mapping[str, Any]) -> None:
    payload = event.get("payload") or {}
    if not isinstance(payload, Mapping):
        return
    snapshot = payload.get("portfolio_after")
    if isinstance(snapshot, Mapping):
        event_sequence = int(event.get("sequence") or 0)

        def _project(current: Any) -> Dict[str, Any]:
            current_value = _normalize(current)
            current_sequence = int(
                current_value.get("event_projection_sequence") or 0
            )
            if current_sequence >= event_sequence:
                return current_value
            projected = _normalize(dict(snapshot))
            projected["event_projection_sequence"] = event_sequence
            return projected

        mutate_json(
            PORTFOLIO_FILE,
            _project,
            default=_default_portfolio(),
        )
    history_record = payload.get("history_record")
    if isinstance(history_record, Mapping):
        _append_unique_projection_record(HISTORY_FILE, history_record)
    cash_flow_record = payload.get("cash_flow_record")
    if isinstance(cash_flow_record, Mapping):
        _append_unique_projection_record(CASHFLOW_FILE, cash_flow_record)


def _recover_portfolio_projection() -> dict[str, Any]:
    projection_existed = os.path.exists(PORTFOLIO_FILE)
    events = signal_ledger.read_events(LEDGER_FILE)
    recovery = event_projection.replay_events(
        events,
        projectors=[_project_portfolio_event],
        checkpoint_file=_projection_checkpoint_file(),
    )
    if recovery.get("status") != "ok":
        return recovery
    expected_events = [
        event
        for event in events
        if isinstance((event.get("payload") or {}).get("portfolio_after"), Mapping)
    ]
    if not projection_existed and expected_events:
        _project_portfolio_event(max(expected_events, key=lambda event: int(event["sequence"])))
    if not _portfolio_projection_matches_ledger():
        return {
            "status": "projection_mismatch",
            "allow_new_risk": False,
        }
    return recovery


def _portfolio_projection_matches_ledger() -> bool:
    actual = _normalize(read_json(PORTFOLIO_FILE, _default_portfolio()))
    return _portfolio_value_matches_ledger(actual)


def _portfolio_value_matches_ledger(actual: Mapping[str, Any]) -> bool:
    expected = event_projection.latest_portfolio_snapshot(
        signal_ledger.read_events(LEDGER_FILE)
    )
    if expected is None:
        return True
    return _portfolio_mutation_state(actual) == _portfolio_mutation_state(expected)


def _portfolio_mutation_state(portfolio: Mapping[str, Any]) -> dict[str, Any]:
    positions = []
    for raw in portfolio.get("positions") or []:
        position = dict(raw)
        positions.append({
            key: position.get(key)
            for key in (
                "code", "name", "cost", "shares", "buy_date", "add_date",
                "strategy_id", "lane", "lots", "sector", "industry",
                "classification_source", "classification_asof",
            )
        })
    return {
        "cash": round(float(portfolio.get("cash") or 0), 2),
        "total_cost": round(float(portfolio.get("total_cost") or 0), 2),
        "positions": positions,
    }


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


def _latest_signal_strategy(code: str) -> Optional[str]:
    """开仓时的车道归属：找该代码最近一条 signal.opened 事件的 strategy_id。

    找不到就是 None（手工建仓/无对应推荐），不臆造车道，时间止损直接跳过。
    """
    try:
        events = signal_ledger.read_events()
    except (OSError, TimeoutError):
        return None
    normalized = str(code).zfill(6)
    for event in reversed(events):
        if event.get("event_type") != "signal.opened":
            continue
        payload = event.get("payload") or {}
        if str(payload.get("code") or "").zfill(6) == normalized:
            return payload.get("strategy_id")
    return None


def _position_lane(strategy_id: Optional[str]) -> Optional[str]:
    if not strategy_id:
        return None
    return "daban" if str(strategy_id).startswith("daban") else "trend"


def _trading_days_elapsed(start: Optional[str], end: str) -> int:
    """start(不含)到 end(含)之间的交易日数，用于打板仓位时间止损。"""
    try:
        cursor = date.fromisoformat(str(start)[:10])
        end_date = date.fromisoformat(str(end)[:10])
    except (TypeError, ValueError):
        return 0
    count = 0
    while cursor < end_date:
        cursor += timedelta(days=1)
        if is_trading_day(cursor):
            count += 1
    return count


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
    portfolio_after: Mapping[str, Any] | None = None,
    cash_flow_record: Mapping[str, Any] | None = None,
    history_record: Mapping[str, Any] | None = None,
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
    payload = {
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
    }
    if portfolio_after is not None:
        payload["portfolio_after"] = dict(portfolio_after)
    if cash_flow_record is not None:
        payload["cash_flow_record"] = dict(cash_flow_record)
    if history_record is not None:
        payload["history_record"] = dict(history_record)
    return signal_ledger.append_event(
        "trade.executed",
        links,
        payload,
        idempotency_key=f"trade.executed:{trade_id}",
        ledger_file=LEDGER_FILE,
    ) or {}


def _record_cash_event(
    action: str,
    amount: float,
    *,
    source: str | None = None,
    asof: str | None = None,
    portfolio_after: Mapping[str, Any] | None = None,
    cash_flow_record: Mapping[str, Any] | None = None,
) -> Dict:
    event_id = signal_ledger.make_trade_execution_id(
        "cash",
        action,
        f"{float(amount):.2f}",
        datetime.now().isoformat(timespec="microseconds"),
    )
    links = signal_ledger.make_links(signal_id=f"cash:{event_id}")
    event_type = {
        "deposit": "cash.deposited",
        "withdraw": "cash.withdrawn",
        "reconcile_cash": "cash.reconciled",
    }[action]
    payload = {
        "action": action,
        "amount": round(float(amount), 2),
        "source": source,
        "asof": asof,
    }
    if portfolio_after is not None:
        payload["portfolio_after"] = dict(portfolio_after)
    if cash_flow_record is not None:
        payload["cash_flow_record"] = dict(cash_flow_record)
    return signal_ledger.append_event(
        event_type,
        links,
        payload,
        idempotency_key=f"{event_type}:{event_id}",
        ledger_file=LEDGER_FILE,
    ) or {}


# ======================== 资金管理 ========================

def _cash_flow_record(
    action: str,
    amount: float,
    note: str,
    *,
    event_id: str,
) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "action": action,
        "amount": round(amount, 2),
        "note": note,
        "timestamp": datetime.now().isoformat(),
    }


def record_cash_flow(
    action: str,
    amount: float,
    note: str = "",
    *,
    event_id: str | None = None,
    record: Mapping[str, Any] | None = None,
) -> Dict:
    """记录资金流水（并发安全追加）。返回流水记录。"""
    value = dict(record) if record is not None else {
        "event_id": event_id,
        "action": action,
        "amount": round(amount, 2),
        "note": note,
        "timestamp": datetime.now().isoformat(),
    }
    if value.get("event_id"):
        _append_unique_projection_record(CASHFLOW_FILE, value)
    else:
        update_json_list(CASHFLOW_FILE, value)
    return value


def _projection_block() -> Dict[str, Any]:
    return {
        "error": "事件账本与持仓投影不一致，禁止资金变更",
        "code": "EVENT_PROJECTION_BLOCKED",
        "blocking_reasons": ["event_projection_unreconciled"],
    }


def _cash_projection_mismatch(
    portfolio: Mapping[str, Any], outcome: Dict[str, Any]
) -> bool:
    if _portfolio_value_matches_ledger(portfolio):
        return False
    outcome.update(_projection_block())
    return True


@_event_transaction
def deposit(amount: float) -> Dict:
    """入金（事务式改现金 + 记流水）。"""
    if amount <= 0:
        return {"error": f"入金金额必须为正: {amount}"}
    if _recover_portfolio_projection().get("status") != "ok":
        return _projection_block()
    outcome: Dict[str, Any] = {}

    def _mut(pf):
        pf = _normalize(pf)
        if _cash_projection_mismatch(pf, outcome):
            return pf
        pf["cash"] = round(pf["cash"] + amount, 2)
        flow = _cash_flow_record(
            "deposit",
            amount,
            f"存入 {amount:,.0f}",
            event_id=signal_ledger.make_trade_execution_id("cash-flow", "deposit"),
        )
        event = _record_cash_event(
            "deposit",
            amount,
            portfolio_after=pf,
            cash_flow_record=flow,
        )
        pf["event_projection_sequence"] = event.get("sequence")
        outcome.update(event=event, cash_flow_record=flow)
        return pf

    pf = mutate_json(PORTFOLIO_FILE, _mut, default=_default_portfolio())
    if not outcome.get("event"):
        return _projection_block()
    event = outcome.get("event") or {}
    record_cash_flow(
        "deposit",
        amount,
        record=outcome["cash_flow_record"],
    )
    event_projection.advance_checkpoint(_projection_checkpoint_file(), event)
    return {
        "ok": True,
        "action": "deposit",
        "amount": round(amount, 2),
        "cash": pf["cash"],
        "event_id": event.get("event_id"),
    }


@_event_transaction
def withdraw(amount: float) -> Dict:
    """出金（事务式校验余额 + 改现金 + 记流水）。余额不足拒绝。"""
    if amount <= 0:
        return {"error": f"出金金额必须为正: {amount}"}
    if _recover_portfolio_projection().get("status") != "ok":
        return _projection_block()
    outcome: Dict = {}

    def _mut(pf):
        pf = _normalize(pf)
        if _cash_projection_mismatch(pf, outcome):
            return pf
        if pf["cash"] < amount:
            outcome["error"] = f"资金不足: 可用{pf['cash']:,.0f}，要取{amount:,.0f}"
            return pf  # 不变更
        pf["cash"] = round(pf["cash"] - amount, 2)
        flow = _cash_flow_record(
            "withdraw",
            amount,
            f"取出 {amount:,.0f}",
            event_id=signal_ledger.make_trade_execution_id("cash-flow", "withdraw"),
        )
        outcome["event"] = _record_cash_event(
            "withdraw",
            amount,
            portfolio_after=pf,
            cash_flow_record=flow,
        )
        pf["event_projection_sequence"] = outcome["event"].get("sequence")
        outcome["cash_flow_record"] = flow
        outcome["ok"] = True
        outcome["cash"] = pf["cash"]
        return pf

    mutate_json(PORTFOLIO_FILE, _mut, default=_default_portfolio())
    if outcome.get("ok"):
        record_cash_flow(
            "withdraw", amount, record=outcome["cash_flow_record"]
        )
        event_projection.advance_checkpoint(
            _projection_checkpoint_file(), outcome.get("event")
        )
        return {
            "ok": True,
            "action": "withdraw",
            "amount": round(amount, 2),
            "cash": outcome["cash"],
            "event_id": (outcome.get("event") or {}).get("event_id"),
        }
    return {
        key: value
        for key, value in outcome.items()
        if key in {"error", "code", "blocking_reasons"}
    }


@_event_transaction
def reconcile_cash(amount: float, *, source: str, asof: str | None = None) -> Dict:
    """Replace runtime cash with a verified balance and append an audit flow."""
    if amount < 0:
        return {"error": f"现金余额不能为负: {amount}"}
    if not str(source or "").strip():
        return {"error": "余额来源不能为空"}
    if _recover_portfolio_projection().get("status") != "ok":
        return _projection_block()
    previous = {"cash": 0.0}

    def _mut(pf):
        pf = _normalize(pf)
        if _cash_projection_mismatch(pf, previous):
            return pf
        previous["cash"] = float(pf.get("cash") or 0)
        pf["cash"] = round(float(amount), 2)
        pf["cash_source"] = str(source).strip()
        pf["cash_asof"] = asof or date.today().isoformat()
        pf["cash_reconciled"] = True
        pf["account_state"] = "verified"
        delta = round(pf["cash"] - previous["cash"], 2)
        flow = _cash_flow_record(
            "reconcile_cash",
            delta,
            f"余额校准为 {pf['cash']:,.2f}，来源={pf['cash_source']}，时点={pf['cash_asof']}",
            event_id=signal_ledger.make_trade_execution_id(
                "cash-flow", "reconcile_cash"
            ),
        )
        previous["event"] = _record_cash_event(
            "reconcile_cash",
            amount,
            source=str(source).strip(),
            asof=asof or date.today().isoformat(),
            portfolio_after=pf,
            cash_flow_record=flow,
        )
        pf["event_projection_sequence"] = previous["event"].get("sequence")
        previous["cash_flow_record"] = flow
        return pf

    portfolio = mutate_json(PORTFOLIO_FILE, _mut, default=_default_portfolio())
    if not previous.get("event"):
        return _projection_block()
    delta = round(portfolio["cash"] - previous["cash"], 2)
    record_cash_flow(
        "reconcile_cash", delta, record=previous["cash_flow_record"]
    )
    event_projection.advance_checkpoint(
        _projection_checkpoint_file(), previous.get("event")
    )
    return {
        "ok": True,
        "action": "reconcile_cash",
        "cash": portfolio["cash"],
        "previous_cash": round(previous["cash"], 2),
        "delta": delta,
        "source": portfolio["cash_source"],
        "asof": portfolio["cash_asof"],
        "event_id": (previous.get("event") or {}).get("event_id"),
    }


# ======================== 交易操作 ========================


def _classification_asof_valid(value: str, acquisition_date: str) -> bool:
    try:
        classified_on = date.fromisoformat(value)
        acquired_date = date.fromisoformat(acquisition_date)
        return classified_on <= acquired_date <= date.today()
    except (TypeError, ValueError):
        return False


def _resolve_position_classification(
    position: Mapping[str, Any] | None,
    *,
    sector: str,
    industry: str,
    source: str,
    asof: str,
    acquired_on: str,
) -> Dict[str, Any]:
    stored_sector = str((position or {}).get("sector") or "").strip()
    stored_industry = str((position or {}).get("industry") or "").strip()
    stored_label = stored_sector or stored_industry
    explicit_label = sector or industry
    resolved = {
        "sector": sector or stored_sector,
        "industry": industry or stored_industry,
        "source": source
        or str((position or {}).get("classification_source") or "").strip(),
        "asof": asof
        or str((position or {}).get("classification_asof") or "").strip(),
    }
    resolved["label"] = resolved["sector"] or resolved["industry"]
    if not resolved["label"]:
        return {"error": "缺少可核验的行业分类，禁止开仓或加仓", "code": "UNKNOWN_SECTOR", "reasons": ["unknown_sector"]}
    if explicit_label and stored_label and explicit_label != stored_label:
        return {
            "error": f"传入行业分类{explicit_label}与已持久化分类{stored_label}冲突",
            "code": "SECTOR_CLASSIFICATION_CONFLICT",
            "reasons": ["sector_classification_conflict"],
        }
    if not resolved["source"] or not resolved["asof"]:
        return {
            "error": "行业分类必须携带明确来源和基准日期",
            "code": "CLASSIFICATION_PROVENANCE_REQUIRED",
            "reasons": ["classification_provenance_required"],
        }
    if not _classification_asof_valid(str(resolved["asof"]), acquired_on):
        return {
            "error": f"行业分类日期非法、来自未来或晚于交易日期: {resolved['asof']}",
            "code": "CLASSIFICATION_DATE_INVALID",
            "reasons": ["classification_date_invalid"],
        }
    return resolved


def _new_position_policy(
    portfolio: Mapping[str, Any],
    *,
    code: str,
    total_cost: float,
    classification: Mapping[str, Any],
) -> Dict[str, Any]:
    policy_portfolio = {
        **portfolio,
        "positions": [dict(position) for position in portfolio["positions"]],
    }
    for position in policy_portfolio["positions"]:
        if position.get("code") == code:
            position.update({
                "sector": classification["sector"],
                "industry": classification["industry"],
                "classification_source": classification["source"],
                "classification_asof": classification["asof"],
            })
    assets = portfolio_value(policy_portfolio)
    proposed_pct = total_cost / assets * 100 if assets > 0 else 0.0
    return evaluate_new_position(
        policy_portfolio,
        code=code,
        sector=str(classification["label"]),
        proposed_position_pct=proposed_pct,
        max_single_position_pct=MAX_SINGLE_POSITION,
        max_sector_exposure_pct=MAX_SECTOR_EXPOSURE,
    )


def _policy_block_code(reasons: list[str]) -> str:
    for reason, code in (
        ("unknown_sector", "UNKNOWN_SECTOR"),
        ("existing_position_sector_unknown", "EXISTING_SECTOR_UNKNOWN"),
        ("sector_exposure_limit", "SECTOR_EXPOSURE_LIMIT"),
        ("single_position_limit", "SINGLE_POSITION_LIMIT"),
    ):
        if reason in reasons:
            return code
    return "PORTFOLIO_POLICY_BLOCKED"


def _apply_purchase(
    portfolio: Dict[str, Any],
    position: Dict[str, Any] | None,
    *,
    code: str,
    name: str,
    cost: float,
    shares: int,
    acquired_on: str,
    strategy_id: str | None,
    lane: str | None,
    classification: Mapping[str, Any],
) -> tuple[Dict[str, Any], str]:
    total_cost = cost * shares
    lot = {"shares": shares, "cost": cost, "acquired_on": acquired_on}
    if position:
        old_total = position["cost"] * position["shares"]
        position["shares"] += shares
        position["cost"] = round((old_total + total_cost) / position["shares"], 2)
        position["add_date"] = acquired_on
        position.setdefault("lots", []).append(lot)
        action = "加仓"
    else:
        position = {
            "code": code, "name": name, "cost": cost, "shares": shares,
            "buy_date": acquired_on, "add_date": acquired_on,
            "peak_price": cost, "strategy_id": strategy_id, "lane": lane,
            "lots": [lot],
        }
        portfolio["positions"].append(position)
        action = "开仓"
    position.update({
        "sector": classification["sector"],
        "industry": classification["industry"],
        "classification_source": classification["source"],
        "classification_asof": classification["asof"],
    })
    portfolio["total_cost"] = round(portfolio["total_cost"] + total_cost, 2)
    portfolio["cash"] = round(portfolio["cash"] - total_cost, 2)
    return position, action


def _record_purchase_projection(
    portfolio: Mapping[str, Any],
    *,
    code: str,
    name: str,
    cost: float,
    shares: int,
    acquired_on: str,
    action: str,
    trade_action: str,
    total_cost: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    flow = _cash_flow_record(
        "buy",
        total_cost,
        f"{action}: {name}({code}) {shares}股 @ {cost}",
        event_id=signal_ledger.make_trade_execution_id(
            "cash-flow", code, acquired_on, "buy"
        ),
    )
    event = _record_trade_execution(
        code=code,
        name=name,
        side="buy",
        price=cost,
        shares=shares,
        trade_date=acquired_on,
        action=trade_action,
        portfolio_after=portfolio,
        cash_flow_record=flow,
    )
    return event, flow


def _locked_lot_block(position: Mapping[str, Any], current_date: str) -> dict[str, Any] | None:
    locked = []
    for lot in position.get("lots", []):
        constraint = t1_constraint(lot.get("acquired_on"), current_date)
        if not constraint["sell_allowed"]:
            locked.append({**lot, "constraint": constraint})
    if not locked:
        return None
    return {
        "earliest_sell_date": max(
            lot["constraint"]["earliest_sell_date"] for lot in locked
        ),
        "locked_shares": sum(int(lot.get("shares") or 0) for lot in locked),
    }


def _holding_days(buy_date: Any, sell_date: str) -> int:
    try:
        return (date.fromisoformat(sell_date) - date.fromisoformat(str(buy_date))).days
    except (TypeError, ValueError):
        return 0

@_event_transaction
def add_position(
    code: str,
    name: str,
    cost: float,
    shares: int,
    trade_date: str | None = None,
    *,
    sector: str | None = None,
    industry: str | None = None,
    classification_source: str | None = None,
    classification_asof: str | None = None,
) -> Dict:
    """开仓/加仓，分类与集中度校验和资金变更在同一把锁内完成。

    行业分类只能来自调用方显式传入的候选/执行证据，禁止按股票名称或代码猜测。
    已有持仓可复用自身已落盘且带来源、日期的分类。
    """
    if cost <= 0 or shares <= 0:
        return {"error": f"价格与股数必须为正: cost={cost}, shares={shares}"}
    try:
        recovery = _recover_portfolio_projection()
    except (OSError, TimeoutError, RuntimeError, ValueError, KeyError):
        recovery = {"status": "replay_required"}
    if recovery.get("status") != "ok":
        return {
            "error": "事件投影未完成或账本对账不一致，禁止新增风险",
            "code": "EVENT_PROJECTION_BLOCKED",
            "blocking_reasons": ["event_projection_unreconciled"],
        }
    total_cost = cost * shares
    acquired_on = trade_date or date.today().isoformat()
    strategy_id = _latest_signal_strategy(code)
    lane = _position_lane(strategy_id)
    outcome: Dict = {}
    explicit_sector = str(sector or "").strip()
    explicit_industry = str(industry or "").strip()
    explicit_label = explicit_sector or explicit_industry
    explicit_source = str(classification_source or "").strip()
    explicit_asof = str(classification_asof or "").strip()

    def _block(error: str, code_value: str, reasons: list[str]) -> None:
        outcome.update({
            "error": error,
            "code": code_value,
            "blocking_reasons": reasons,
        })

    def _mut(pf):
        pf = _normalize(pf)
        if not _portfolio_value_matches_ledger(pf):
            outcome.update({
                "error": "事件账本与持仓投影不一致，禁止新增风险",
                "code": "EVENT_PROJECTION_BLOCKED",
                "blocking_reasons": ["event_projection_unreconciled"],
            })
            return pf
        if pf.get("new_risk_blocked") and "valuation_unknown" in (
            pf.get("risk_blocking_reasons") or []
        ):
            outcome.update({
                "error": "持仓行情不完整，组合估值未知；恢复全部持仓行情前禁止开仓或加仓",
                "code": "VALUATION_UNKNOWN",
                "blocking_reasons": ["valuation_unknown"],
            })
            return pf
        pos_found = next((p for p in pf["positions"] if p["code"] == code), None)
        classification = _resolve_position_classification(
            pos_found,
            sector=explicit_sector,
            industry=explicit_industry,
            source=explicit_source,
            asof=explicit_asof,
            acquired_on=acquired_on,
        )
        if classification.get("error"):
            _block(classification["error"], classification["code"], classification["reasons"])
            return pf
        policy = _new_position_policy(
            pf, code=code, total_cost=total_cost, classification=classification,
        )
        if not policy["allowed"]:
            reasons = list(policy.get("reasons") or [])
            _block(
                f"组合集中度门禁阻断: {', '.join(reasons)}",
                _policy_block_code(reasons),
                reasons,
            )
            outcome["policy"] = policy
            return pf

        if pf["cash"] < total_cost:
            outcome["error"] = f"可用资金不足: 需要{total_cost:,.0f}，可用{pf['cash']:,.0f}"
            return pf  # 不变更

        trade_action = "add" if pos_found else "open"
        pos_found, action = _apply_purchase(
            pf, pos_found, code=code, name=name, cost=cost, shares=shares,
            acquired_on=acquired_on, strategy_id=strategy_id, lane=lane,
            classification=classification,
        )
        event, flow = _record_purchase_projection(
            pf,
            code=code,
            name=name,
            cost=cost,
            shares=shares,
            acquired_on=acquired_on,
            action=action,
            trade_action=trade_action,
            total_cost=total_cost,
        )
        outcome.update(_trade_event=event, _cash_flow_record=flow)
        pf["event_projection_sequence"] = outcome["_trade_event"].get("sequence")
        outcome.update(
            ok=True,
            action=action,
            cost=pos_found["cost"],
            shares=pos_found["shares"],
            cash_remaining=pf["cash"],
            sector=classification["sector"],
            industry=classification["industry"],
            classification_source=classification["source"],
            classification_asof=classification["asof"],
        )
        return pf

    mutate_json(PORTFOLIO_FILE, _mut, default=_default_portfolio())
    if not outcome.get("ok"):
        return {
            key: value
            for key, value in outcome.items()
            if key in {"error", "code", "blocking_reasons", "policy"}
        }
    trade_event = outcome.pop("_trade_event", {})
    record_cash_flow(
        "buy", total_cost, record=outcome.pop("_cash_flow_record")
    )
    event_projection.advance_checkpoint(_projection_checkpoint_file(), trade_event)
    monitor_registry.activate(
        "stock",
        code,
        name,
        source="portfolio_buy",
        force=True,
        metadata={
            "position_linked": True,
            "sector": outcome.get("sector"),
            "industry": outcome.get("industry"),
            "classification_source": outcome.get("classification_source"),
            "classification_asof": outcome.get("classification_asof"),
            **(trade_event.get("links") or _latest_stock_links(code)),
        },
    )
    return {"ok": True, "code": code, "name": name, **outcome}


def _earliest_acquisition(position: Mapping[str, Any]) -> str:
    """最早建仓日；分类基准日必须不晚于它才算「当时可核验」。"""
    dates = [
        str(lot.get("acquired_on"))
        for lot in (position.get("lots") or [])
        if lot.get("acquired_on")
    ]
    dates.extend(
        str(value) for value in (position.get("buy_date"),) if value
    )
    return min(dates) if dates else "1970-01-01"


def _apply_reclassification(
    position: Dict | None,
    *,
    code: str,
    sector: str,
    industry: str,
    source: str,
    asof: str,
) -> Dict:
    """校验并写入分类；返回 outcome（成功带 ok，失败带 error/code）。"""
    if position is None:
        return {
            "error": f"未持有 {code}，无法补分类",
            "code": "POSITION_NOT_FOUND",
        }
    classification = _resolve_position_classification(
        position,
        sector=sector,
        industry=industry,
        source=source,
        asof=asof,
        acquired_on=_earliest_acquisition(position),
    )
    if classification.get("error"):
        return {
            "error": classification["error"],
            "code": classification["code"],
            "blocking_reasons": classification["reasons"],
        }
    resolved = {
        "sector": classification["sector"],
        "industry": classification["industry"],
        "classification_source": classification["source"],
        "classification_asof": classification["asof"],
    }
    position.update(resolved)
    return {"ok": True, "name": position.get("name"), **resolved}


@_event_transaction
def reclassify_position(
    code: str,
    *,
    sector: str | None = None,
    industry: str | None = None,
    classification_source: str | None = None,
    classification_asof: str | None = None,
) -> Dict:
    """给缺分类的历史持仓补齐 sector/industry，不改股数、现金与事件账本。

    2026-07 之前手工导入的持仓没有分类字段，集中度检查无法核验板块重叠
    （issue #172）。这里只补空缺：已落盘的分类一律不覆盖，改分类必须清仓重开。
    补齐后既有板块暴露可能已经超限，那是既存风险被看见而非本次动作产生的，
    因此不在这里阻断，由后续开仓检查照常拦截。
    """
    normalized_code = str(code or "").strip()
    if not normalized_code:
        return {"error": "缺少股票代码", "code": "CODE_REQUIRED"}
    outcome: Dict = {}

    def _mut(pf):
        pf = _normalize(pf)
        outcome.update(_apply_reclassification(
            next(
                (item for item in pf["positions"] if item["code"] == normalized_code),
                None,
            ),
            code=normalized_code,
            sector=str(sector or "").strip(),
            industry=str(industry or "").strip(),
            source=str(classification_source or "").strip(),
            asof=str(classification_asof or "").strip(),
        ))
        return pf

    mutate_json(PORTFOLIO_FILE, _mut, default=_default_portfolio())
    if not outcome.get("ok"):
        return {
            key: value
            for key, value in outcome.items()
            if key in {"error", "code", "blocking_reasons"}
        }
    monitor_registry.activate(
        "stock",
        normalized_code,
        str(outcome.get("name") or normalized_code),
        source="portfolio_reclassify",
        force=True,
        metadata={
            "position_linked": True,
            "sector": outcome.get("sector"),
            "industry": outcome.get("industry"),
            "classification_source": outcome.get("classification_source"),
            "classification_asof": outcome.get("classification_asof"),
            **_latest_stock_links(normalized_code),
        },
    )
    return {"code": normalized_code, **outcome}


@_event_transaction
def close_position(
    code: str,
    sell_price: float,
    trade_date: str | None = None,
) -> Dict:
    """清仓（事务式：加回现金 + 移除持仓，全程单锁；落盘后再记历史/流水）。"""
    if sell_price <= 0:
        return {"error": f"卖出价必须为正: {sell_price}"}
    try:
        recovery = _recover_portfolio_projection()
    except (OSError, TimeoutError, RuntimeError, ValueError, KeyError):
        recovery = {"status": "replay_required"}
    if recovery.get("status") != "ok":
        return {
            "error": "事件投影未完成或账本对账不一致，禁止修改持仓",
            "code": "EVENT_PROJECTION_BLOCKED",
        }
    current_date = trade_date or date.today().isoformat()
    outcome: Dict = {}

    def _mut(pf):
        pf = _normalize(pf)
        if not _portfolio_value_matches_ledger(pf):
            outcome.update({
                "error": "事件账本与持仓投影不一致，禁止修改持仓",
                "code": "EVENT_PROJECTION_BLOCKED",
            })
            return pf
        idx = next((i for i, p in enumerate(pf["positions"]) if p["code"] == code), None)
        if idx is None:
            outcome["error"] = f"未找到持仓: {code}"
            return pf

        pos = pf["positions"][idx]
        locked = _locked_lot_block(pos, current_date)
        if locked:
            outcome.update({
                "error": f"A股T+1限制：{code}含当日买入/加仓股份，最早{locked['earliest_sell_date']}可全部卖出",
                "code": "T1_LOCKED",
                **locked,
            })
            return pf
        proceeds = sell_price * pos["shares"]
        cost_basis = pos["cost"] * pos["shares"]
        pnl = proceeds - cost_basis
        pnl_pct = (sell_price / pos["cost"] - 1) * 100

        hold_days = _holding_days(pos.get("buy_date"), current_date)

        history_record = {
            "event_id": signal_ledger.make_trade_execution_id(
                "history", code, current_date, "close"
            ),
            "code": code, "name": pos["name"],
            "buy_date": pos["buy_date"], "sell_date": current_date,
            "cost": pos["cost"], "sell_price": sell_price, "shares": pos["shares"],
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 1),
            "hold_days": hold_days,
        }
        outcome["record"] = history_record
        pf["total_cost"] = max(round(pf["total_cost"] - cost_basis, 2), 0)
        pf["cash"] = round(pf["cash"] + proceeds, 2)
        pf["positions"].pop(idx)
        flow = _cash_flow_record(
            "sell",
            proceeds,
            f"清仓: {history_record['name']}({code}) 盈亏{history_record['pnl_pct']:+.1f}%",
            event_id=signal_ledger.make_trade_execution_id(
                "cash-flow", code, current_date, "sell"
            ),
        )
        outcome["_trade_event"] = _record_trade_execution(
            code=code,
            name=history_record["name"],
            side="sell",
            price=sell_price,
            shares=history_record["shares"],
            trade_date=current_date,
            action="close",
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 1),
            portfolio_after=pf,
            cash_flow_record=flow,
            history_record=history_record,
        )
        pf["event_projection_sequence"] = outcome["_trade_event"].get(
            "sequence"
        )
        outcome["_cash_flow_record"] = flow
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
    trade_event = outcome.pop("_trade_event", {})
    _append_unique_projection_record(HISTORY_FILE, rec)
    record_cash_flow(
        "sell", outcome["proceeds"], record=outcome.pop("_cash_flow_record")
    )
    event_projection.advance_checkpoint(_projection_checkpoint_file(), trade_event)
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
        if not isinstance(quote, dict):
            return None
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


def _apply_prices(pf: Dict, fetched: Dict[str, Optional[Dict]],
                  deep_scores: Optional[Dict[str, float | Mapping[str, Any]]] = None) -> Dict:
    """把预取到的现价合并进持仓并算风控告警。fetched: code -> 行情或 None。

    止损执行闭环（issue #88）：止损首次触发时把 stop_loss_triggered_on 落进
    持仓，此后每次检查按逾期交易日数升级告警——止损建议只发一次然后被无视，
    是 -5% 拖成 -25% 的直接原因。deep_scores 携带的是 agent 研究意见，低分
    只能触发复核；没有新鲜、结构化且独立验证的硬风险证据时不得生成交易动作。
    """
    alerts = []
    total_value = 0
    stale_market_value = 0
    missing_quote_codes = []
    today = date.today().isoformat()
    deep_scores = deep_scores or {}

    for pos in pf["positions"]:
        # A single unknown holding makes every exact portfolio weight unknowable.
        # Clear persisted weights before evaluating this refresh.
        pos["weight_pct"] = None
        t1_state = _position_t1_state(pos)
        pos.update(t1_state)
        data = fetched.get(pos["code"])
        if data and data.get("price"):
            pos["current_price"] = data["price"]
            pos["change_pct"] = data.get("change_pct")
            pos["price_fetched_at"] = data.get("fetched_at") or datetime.now().astimezone().isoformat()
            pos["price_stale"] = False
            pos["quote_status"] = "fresh"
            pos["valuation_status"] = "fresh"
            pos["market_value"] = data["price"] * pos["shares"]
            pos["pnl"] = round((data["price"] - pos["cost"]) * pos["shares"], 2)
            pos["pnl_pct"] = round((data["price"] / pos["cost"] - 1) * 100, 1)

            if data["price"] > pos.get("peak_price", 0):
                pos["peak_price"] = data["price"]

            total_value += pos["market_value"]

            if pos["pnl_pct"] <= STOP_LOSS_PCT:
                if not pos.get("stop_loss_triggered_on"):
                    pos["stop_loss_triggered_on"] = today
                overdue_days = _trading_days_elapsed(
                    pos["stop_loss_triggered_on"], today
                )
                if t1_state["locked_shares"]:
                    message = (
                        f"{pos['name']}({pos['code']}) 浮亏{pos['pnl_pct']}%，风险已触发；"
                        f"{t1_state['locked_shares']}股受A股T+1锁定，最早"
                        f"{t1_state['earliest_sell_date']}处置"
                    )
                    level = "🔴 止损"
                elif overdue_days >= 1:
                    message = (
                        f"{pos['name']}({pos['code']}) 止损已于"
                        f"{pos['stop_loss_triggered_on']}触发，逾期{overdue_days}个交易日"
                        f"仍未执行！当前浮亏{pos['pnl_pct']}%。止损必须机械执行，"
                        f"每拖一天亏损可能扩大（-5%拖成-25%的教训）"
                    )
                    level = "🔴🔴 止损逾期"
                else:
                    message = (
                        f"{pos['name']}({pos['code']}) 浮亏{pos['pnl_pct']}%，触发硬止损！"
                        f"成本{pos['cost']}，现价{data['price']}，今日必须执行卖出"
                    )
                    level = "🔴 止损"
                alerts.append({
                    "level": level,
                    "msg": message,
                    "execution_status": "t1_locked" if t1_state["locked_shares"] else "sellable",
                    "stop_loss_triggered_on": pos["stop_loss_triggered_on"],
                    "overdue_trading_days": overdue_days,
                    **t1_state,
                })
            elif pos.get("stop_loss_triggered_on"):
                # 价格回到止损线上方，解除触发状态（避免陈旧升级告警）。
                pos.pop("stop_loss_triggered_on", None)

            deep_record = deep_scores.get(str(pos["code"]).zfill(6))
            deep_score = (
                deep_record.get("deep_score")
                if isinstance(deep_record, Mapping)
                else deep_record
            )
            if isinstance(deep_score, (int, float)) and deep_score < 5:
                stale = bool(
                    isinstance(deep_record, Mapping) and deep_record.get("stale")
                )
                freshness_status = (
                    str(deep_record.get("freshness_status") or ("stale" if stale else "fresh"))
                    if isinstance(deep_record, Mapping)
                    else "unknown"
                )
                freshness_note = "且缓存已过期，" if stale else "，"
                alerts.append({
                    "level": "🟠 深研复核",
                    "category": "research_review",
                    "reason_code": "deep_research_review_required",
                    "msg": (
                        f"{pos['name']}({pos['code']}) 深研评分{deep_score:.1f}/10"
                        f"低于复核线5.0{freshness_note}该评分未绑定可执行硬风险证据，"
                        "仅要求研究复核，不构成减仓或清仓指令"
                    ),
                    "execution_status": "review_required",
                    "deep_score": deep_score,
                    "review_required": True,
                    "execution_eligible": False,
                    "evidence_status": "unbound_score",
                    "freshness_status": freshness_status,
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

            if pos["pnl_pct"] >= TAKE_PROFIT_PCT:
                alerts.append({
                    "level": "🟢 止盈目标",
                    "msg": (
                        f"{pos['name']}({pos['code']}) 浮盈{pos['pnl_pct']}%，"
                        f"已达到止盈目标{TAKE_PROFIT_PCT}%，建议分批了结，不要等回撤止盈才动"
                    ),
                    "execution_status": "t1_locked" if t1_state["locked_shares"] else "sellable",
                    **t1_state,
                })

            if pos.get("lane") == "daban":
                held_trading_days = _trading_days_elapsed(pos.get("buy_date"), date.today().isoformat())
                if held_trading_days >= POSITION_TIME_STOP_DAYS:
                    alerts.append({
                        "level": "🟠 时间止损",
                        "msg": (
                            f"{pos['name']}({pos['code']}) 打板来源仓位已持有{held_trading_days}个"
                            f"交易日，超过{POSITION_TIME_STOP_DAYS}天时间止损线，无论盈亏({pos['pnl_pct']}%)"
                            f"建议了结，不要把打板仓位捂成波段"
                        ),
                        "execution_status": "t1_locked" if t1_state["locked_shares"] else "sellable",
                        **t1_state,
                    })
        else:
            last_price = pos.get("current_price")
            last_market_value = pos.get("market_value")
            has_stale_reference = (
                isinstance(last_price, (int, float)) and last_price > 0
            ) or (
                isinstance(last_market_value, (int, float)) and last_market_value > 0
            )
            pos["change_pct"] = None
            pos["price_stale"] = has_stale_reference
            pos["quote_status"] = "unavailable"
            pos["valuation_status"] = "stale" if has_stale_reference else "unknown"
            missing_quote_codes.append(str(pos["code"]).zfill(6))
            if isinstance(last_market_value, (int, float)) and last_market_value > 0:
                stale_market_value += last_market_value
            reference_note = (
                f"；仅保留最后价格{last_price}作为陈旧参考，不参与精确仓位计算"
                if isinstance(last_price, (int, float)) and last_price > 0
                else "；无可用的最后价格"
            )
            alerts.append({
                "level": "🔴 数据质量",
                "category": "data_quality",
                "reason_code": "valuation_unknown",
                "code": str(pos["code"]).zfill(6),
                "blocks_new_risk": True,
                "execution_status": "blocked",
                "msg": (
                    f"valuation_unknown: {pos['name']}({pos['code']}) 行情获取失败，"
                    f"组合估值及仓位权重不可可靠计算{reference_note}；恢复行情前禁止新增风险"
                ),
            })

    # 仓位集中度（按总资产 = 现金 + 持仓市值）
    if not missing_quote_codes and total_value > 0:
        total_asset = pf.get("cash", 0) + total_value
        for pos in pf["positions"]:
            weight = pos.get("market_value", 0) / total_asset * 100
            pos["weight_pct"] = round(weight, 1)
            if weight > MAX_SINGLE_POSITION:
                alerts.append({
                    "level": "🟡 风控",
                    "msg": f"{pos['name']} 仓位{weight:.0f}%，超过单只上限{MAX_SINGLE_POSITION}%"
                })

    valuation_unknown = bool(missing_quote_codes)
    blocking_reasons = ["valuation_unknown"] if valuation_unknown else []
    data_quality = {
        "status": "blocked" if valuation_unknown else "complete",
        "missing_quote_codes": missing_quote_codes,
    }
    pf["valuation_status"] = "unknown" if valuation_unknown else "complete"
    pf["new_risk_blocked"] = valuation_unknown
    pf["risk_blocking_reasons"] = blocking_reasons
    pf["data_quality"] = data_quality
    return {
        "alerts": alerts,
        "total_value": None if valuation_unknown else round(total_value, 2),
        "known_market_value": round(total_value, 2),
        "stale_market_value": round(stale_market_value, 2),
        "valuation_status": pf["valuation_status"],
        "new_risk_blocked": valuation_unknown,
        "blocking_reasons": blocking_reasons,
        "data_quality": data_quality,
    }


def refresh_prices() -> tuple:
    """拉取现价（锁外网络IO）→ 在事务内合并刷价。返回 (持仓, 风控结果)。

    现价请求放在锁外，避免持锁期间阻塞在 HTTP；合并写回放在 mutate 事务内，
    期间被并发开/清仓改动的持仓表不会被这次刷价覆盖丢失。
    """
    snapshot = ensure_portfolio()
    fetched = {pos["code"]: fetch_price(pos["code"]) for pos in snapshot["positions"]}
    deep_scores: Dict[str, Mapping[str, Any]] = {}
    for pos in snapshot["positions"]:
        try:
            from deep_research_cache import read_deep_research
            record = read_deep_research(pos["code"])
            if record and isinstance(record.get("deep_score"), (int, float)):
                deep_scores[str(pos["code"]).zfill(6)] = record
        except Exception:  # noqa: BLE001 — 深研缓存缺失不阻塞风控检查
            continue
    result: Dict = {}

    def _mut(pf):
        pf = _normalize(pf)
        result.update(_apply_prices(pf, fetched, deep_scores))
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
    total_cost = pf.get("total_cost", 0)
    valuation_unknown = result.get("valuation_status") == "unknown" or total_value is None

    lines.append(f"💵 **可用资金:** {cash:,.0f}")
    if valuation_unknown:
        known_value = result.get("known_market_value", 0) or 0
        stale_value = result.get("stale_market_value", 0) or 0
        lines.append(
            f"📦 持仓市值: 估值未知 | 已知新鲜市值: {known_value:,.0f} | 持仓成本: {total_cost:,.0f}"
        )
        if stale_value:
            lines.append(f"🕒 陈旧参考市值: {stale_value:,.0f}（不参与精确总资产或仓位计算）")
        lines.append("📈 浮动盈亏: **估值未知**")
        lines.append("💰 **总资产: 估值未知**")
        lines.append("⛔ 新增风险已阻断: valuation_unknown")
    else:
        total_pnl = sum(p.get("pnl", 0) for p in pf["positions"])
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
        if pos.get("quote_status") == "unavailable":
            price_text = f"{price}（陈旧）" if price not in {None, "?"} else "?"
            market_value = pos.get("market_value")
            market_value_text = (
                f"{market_value:,.0f}（陈旧）"
                if isinstance(market_value, (int, float)) and market_value > 0
                else "?"
            )
            lines.append(
                f"| {pos['name']} | {pos['cost']} | {price_text} | ? | "
                f"{market_value_text} | ? | ⚠️行情缺失 |"
            )
        else:
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
        if pf.get("valuation_status") == "unknown" or any(
            position.get("quote_status") == "unavailable"
            for position in pf["positions"]
        ):
            stale_mv = sum(
                position.get("market_value", 0) or 0
                for position in pf["positions"]
                if position.get("quote_status") == "unavailable"
            )
            lines.append("📊 持仓市值: 估值未知")
            if stale_mv:
                lines.append(f"🕒 陈旧参考市值: {stale_mv:,.0f}（非精确估值）")
            lines.append("💰 总资产: **估值未知**")
            lines.append("⛔ 新增风险已阻断: valuation_unknown")
        else:
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
    p.add_argument("--sector", help="行业/板块分类（必须与来源、日期同时提供）")
    p.add_argument("--industry", help="细分行业分类（可选）")
    p.add_argument("--classification-source", help="分类来源，如 candidate_snapshot")
    p.add_argument("--classification-asof", help="分类基准日 YYYY-MM-DD")
    p.add_argument(
        "--reclassify",
        metavar="CODE",
        help="给缺分类的历史持仓补齐行业（需同时给 --sector/--industry 与来源、日期）",
    )
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

    elif args.reclassify:
        result = reclassify_position(
            args.reclassify,
            sector=args.sector,
            industry=args.industry,
            classification_source=args.classification_source,
            classification_asof=args.classification_asof,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif result.get("error"):
            print(f"❌ {result['error']}")
            sys.exit(1)
        else:
            print(f"✅ 已补分类: {result.get('name', '')}({result['code']}) "
                  f"{result.get('sector', '')}/{result.get('industry', '')} "
                  f"| 来源: {result.get('classification_source', '')} "
                  f"@ {result.get('classification_asof', '')}")
        sys.exit(1 if result.get("error") else 0)

    elif args.add:
        code, name, cost = args.add
        result = add_position(
            code,
            name,
            float(cost),
            args.shares,
            sector=args.sector,
            industry=args.industry,
            classification_source=args.classification_source,
            classification_asof=args.classification_asof,
        )
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
