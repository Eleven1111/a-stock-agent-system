#!/usr/bin/env python3
"""
推荐审计档案 — 买卖建议记录 / 查询 / 结果更新 / 仓位测算
========================================================
推荐文件: $HERMES_HOME/skills/stock-triage/data/recommendations.json
交易历史: $HERMES_HOME/skills/stock-triage/data/trade_history.json

Usage:
  python3 recommendation_audit.py --record 002156 通富微电 buy "10.80-11.00" "半导体主线早盘回封"
  python3 recommendation_audit.py --list
  python3 recommendation_audit.py --code 002156 --json
  python3 recommendation_audit.py --update REC_ID profit --pnl 8.5
  python3 recommendation_audit.py --example --json
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, date
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
from state_store import read_json, update_json_list, mutate_json
from paths import data_file
from a_share_rules import t1_constraint
from recommendation_quality import build_quality_report
from decision_policy import evaluate_decision
from market_context import market_regime, read_market_context
from portfolio_policy import evaluate_candidate
from research_evidence import build_research_evidence, strategy_attributions
import signal_ledger
import strategy_registry


RECOMMENDATIONS_FILE = data_file("stock-triage", "recommendations.json")
HISTORY_FILE = data_file("stock-triage", "trade_history.json")
PORTFOLIO_FILE = data_file("stock-triage", "portfolio.json")
LEDGER_FILE = signal_ledger.LEDGER_FILE

VALID_ACTIONS = {"buy", "sell", "add", "reduce", "hold", "avoid"}
VALID_OUTCOMES = {"pending", "profit", "loss", "breakeven", "invalidated"}

DEFAULT_POSITION_RANGES = {
    "daban": (3.0, 5.0),
    "daban:first_board_reseal": (3.0, 5.0),
    "daban:second_board_weak_to_strong": (3.0, 5.0),
    "trend_pullback": (5.0, 8.0),
    "washout_pickup": (4.0, 6.0),
}


def load_recommendations() -> List[Dict[str, Any]]:
    return read_json(RECOMMENDATIONS_FILE, [])


def load_trade_history() -> List[Dict[str, Any]]:
    legacy = read_json(HISTORY_FILE, [])
    canonical = [
        {
            **record,
            "pnl": record.get("t1_close_ret", record.get("pnl_pct")),
        }
        for record in signal_ledger.project_signals(ledger_file=LEDGER_FILE)
        if record.get("outcome") not in {None, "pending"}
    ]
    seen = {
        (
            record.get("signal_id"),
            record.get("trade_id"),
            record.get("recommendation_id"),
        )
        for record in canonical
    }
    for record in legacy if isinstance(legacy, list) else []:
        identity = (
            record.get("signal_id"),
            record.get("trade_id"),
            record.get("recommendation_id"),
        )
        if any(identity) and identity in seen:
            continue
        canonical.append(record)
        if any(identity):
            seen.add(identity)
    return canonical


def _clean_list(values: Optional[List[str]]) -> List[str]:
    return [str(v).strip() for v in (values or []) if str(v).strip()]


def _make_id(code: str) -> str:
    return f"rec-{date.today().isoformat()}-{code}-{uuid.uuid4().hex[:8]}"


def _sell_t1_block(code: str, asof: str) -> Dict[str, Any] | None:
    portfolio = read_json(PORTFOLIO_FILE, {})
    positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else []
    position = next((item for item in positions if str(item.get("code")).zfill(6) == code), None)
    if not position:
        return None
    known_dates = [
        str(value)
        for value in (position.get("buy_date"), position.get("add_date"))
        if value
    ]
    lots = position.get("lots") or [{
        "shares": position.get("shares"),
        "acquired_on": max(known_dates) if known_dates else "1970-01-01",
    }]
    locked = [
        {**lot, "constraint": t1_constraint(lot.get("acquired_on"), asof)}
        for lot in lots
        if not t1_constraint(lot.get("acquired_on"), asof)["sell_allowed"]
    ]
    if not locked:
        return None
    return {
        "error": "A股T+1限制：当日买入/加仓股份不能当日卖出",
        "code": "T1_LOCKED",
        "earliest_sell_date": max(
            item["constraint"]["earliest_sell_date"]
            for item in locked
        ),
        "locked_shares": sum(int(item.get("shares") or 0) for item in locked),
    }


def calculate_odds(entry_price: Optional[float], target_price: Optional[float], stop_price: Optional[float]) -> Optional[float]:
    """赔率 b = 预期盈利 / 可能亏损。价格不完整或亏损边界无效时返回 None。"""
    if entry_price is None or target_price is None or stop_price is None:
        return None
    possible_loss = entry_price - stop_price
    expected_profit = target_price - entry_price
    if possible_loss <= 0 or expected_profit <= 0:
        return None
    return round(expected_profit / possible_loss, 4)


def strategy_stats(strategy_id: Optional[str], history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """按策略类型统计历史胜率。兼容 strategy_id / strategy_type 两种历史字段。"""
    sid = strategy_id or "default"
    records = history if history is not None else load_trade_history()
    matched = [
        r for r in records
        if (r.get("strategy_id") or r.get("strategy_type") or "default") == sid
    ]
    wins = [
        r for r in matched
        if r.get("outcome") in {"profit", "win", "win_big"} or float(r.get("pnl", 0) or 0) > 0
    ]
    losses = [
        r for r in matched
        if r.get("outcome") in {"loss", "loss_big"} or float(r.get("pnl", 0) or 0) < 0
    ]
    total = len(matched)
    return {
        "strategy_id": sid,
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total, 4) if total else None,
    }


def position_guidance(
    strategy_id: Optional[str],
    entry_price: Optional[float],
    target_price: Optional[float],
    stop_price: Optional[float],
    total_asset: float = 100000.0,
) -> Dict[str, Any]:
    odds = calculate_odds(entry_price, target_price, stop_price)
    stats = strategy_stats(strategy_id)
    sid = strategy_id or "default"

    guidance: Dict[str, Any] = {
        "strategy_id": sid,
        "odds_b": odds,
        "history_total": stats["total"],
        "history_win_rate": stats["win_rate"],
        "method": "startup_default",
        "kelly_fraction": None,
        "execution_fraction": None,
        "recommended_position_pct": None,
        "recommended_amount": None,
    }

    # 实盘门控：被 strategy_registry 停用的策略 → 仓位归零（淘汰负期望策略，胜率闭环的执行点）
    try:
        import strategy_registry as _sr
        _gate_rec = _sr.get(sid)
    except Exception:  # noqa: BLE001
        _gate_rec = None
    if _gate_rec and _gate_rec.get("gating_status") == "disabled":
        guidance.update({
            "method": "gated_off",
            "kelly_fraction": 0.0,
            "execution_fraction": 0.0,
            "recommended_position_pct": 0.0,
            "recommended_amount": 0.0,
            "gating_status": "disabled",
            "reason": f"策略 {sid} 已被实盘门控停用（期望值转负），暂停建仓",
        })
        return guidance

    # 情绪温度倍率：打板范式仓位随市场温度缩放（冰点0.3/发酵1.0/极热0，退潮信号归零）。
    # 温度数据缺失 → 1.0 不影响；趋势/中线策略不受打板情绪温度约束。
    temp_multiplier = 1.0
    temp_tier = None
    temp_allow_new = True
    if sid.startswith("daban"):
        try:
            from market_temperature import read_temperature

            temp = read_temperature(
                event_asof=date.today().isoformat(),
                max_age_days=4,
            )
            if temp.get("tier") != "neutral":
                temp_allow_new = bool(temp.get("allow_new_daban", True))
                temp_multiplier = (
                    float(temp.get("position_multiplier", 1.0))
                    if temp_allow_new
                    else 0.0
                )
                temp_tier = temp.get("tier")
        except Exception:  # noqa: BLE001
            pass

    if odds is not None and stats["win_rate"] is not None and stats["total"] >= 10:
        p = stats["win_rate"]
        q = 1 - p
        kelly = max(0.0, (odds * p - q) / odds)
        execution = kelly / 2 if stats["total"] >= 20 else kelly / 4
        execution *= temp_multiplier
        guidance.update({
            "method": "kelly_half" if stats["total"] >= 20 else "kelly_quarter",
            "kelly_fraction": round(kelly, 4),
            "execution_fraction": round(execution, 4),
            "recommended_position_pct": round(execution * 100, 2),
            "recommended_amount": round(total_asset * execution, 2),
        })
        if temp_tier:
            guidance["temperature"] = {
                "tier": temp_tier,
                "multiplier": temp_multiplier,
                "allow_new_daban": temp_allow_new,
            }
            if not temp_allow_new:
                guidance["reason"] = f"{temp_tier}阶段禁止新开打板仓"
        return guidance

    lo, hi = DEFAULT_POSITION_RANGES.get(sid, DEFAULT_POSITION_RANGES.get(sid.split(":", 1)[0], (3.0, 5.0)))
    midpoint = (lo + hi) / 2 * temp_multiplier
    guidance.update({
        "default_position_range_pct": [lo, hi],
        "recommended_position_pct": round(midpoint, 2),
        "recommended_amount": round(total_asset * midpoint / 100, 2),
        "reason": "历史样本不足10笔或价格条件不足，使用启动阶段默认仓位",
    })
    if temp_tier:
        guidance["temperature"] = {
            "tier": temp_tier,
            "multiplier": temp_multiplier,
            "allow_new_daban": temp_allow_new,
        }
        if not temp_allow_new:
            guidance["reason"] = f"{temp_tier}阶段禁止新开打板仓"
    return guidance


def record_recommendation(
    code: str,
    name: str,
    action: str,
    price_range: str,
    rationale: str,
    risks: Optional[List[str]] = None,
    entry_price: Optional[float] = None,
    target_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    horizon: Optional[str] = None,
    grade: Optional[str] = None,
    confidence: Optional[str] = None,
    strategy_id: Optional[str] = None,
    total_asset: float = 100000.0,
    announcements: Optional[List[Dict[str, Any]]] = None,
    source_id: Optional[str] = None,
    asof: Optional[str] = None,
    correlation_id: Optional[str] = None,
    signal_id: Optional[str] = None,
    trade_id: Optional[str] = None,
    monitor_id: Optional[str] = None,
    research_evidence: Optional[Dict[str, Any]] = None,
    portfolio_risk: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    code = str(code).zfill(6)
    action = action.lower().strip()
    if action not in VALID_ACTIONS:
        return {"error": f"非法建议动作: {action}"}
    if not name or not price_range or not rationale:
        return {"error": "name、price_range、rationale 为必填字段"}
    record_date = asof or date.today().isoformat()
    if action in {"sell", "reduce"}:
        t1_block = _sell_t1_block(code, record_date)
        if t1_block:
            return t1_block

    sizing = position_guidance(
        strategy_id,
        entry_price,
        target_price,
        stop_price,
        total_asset,
    )
    quality = build_quality_report(
        {
            "code": code,
            "name": name,
            "action": action,
            "entry_price": entry_price,
            "price_range": price_range,
            "stop_price": stop_price,
            "target_price": target_price,
            "horizon": horizon,
            "grade": grade,
            "confidence": confidence,
            "position_pct": sizing.get("recommended_position_pct"),
            "tradeability": {"tradeable": True, "status": "not_checked"},
        },
        announcements,
        asof=record_date,
    )
    sid = strategy_id or "default"
    lane = "daban" if sid.startswith("daban") else "trend"
    evidence = research_evidence or build_research_evidence(
        code,
        strategy_id=sid,
        asof=record_date,
    )
    risk = portfolio_risk or evaluate_candidate(
        read_json(PORTFOLIO_FILE, {"cash": total_asset, "positions": []}),
        {"code": code},
        float(sizing.get("recommended_position_pct") or 0),
    )
    policy = evaluate_decision(
        requested_action=action,
        quality_report=quality,
        strategy_record=strategy_registry.get(sid),
        market_regime=market_regime(read_market_context()),
        portfolio_risk=risk,
        research_evidence=evidence,
        strategy_lane=lane,
    )
    effective_action = (
        "avoid"
        if policy["decision"] == "avoid"
        else "hold"
        if policy["decision"] == "watch"
        else action
    )
    if policy["position_multiplier"] == 0:
        sizing.update({
            "recommended_position_pct": 0.0,
            "recommended_amount": 0.0,
            "policy_blocked": True,
        })
    elif policy["position_multiplier"] < 1:
        sizing["recommended_position_pct"] = round(
            float(sizing.get("recommended_position_pct") or 0)
            * float(policy["position_multiplier"]),
            2,
        )
        sizing["recommended_amount"] = round(
            float(sizing.get("recommended_amount") or 0)
            * float(policy["position_multiplier"]),
            2,
        )
    opens_signal = (
        effective_action in signal_ledger.SETTLEABLE_ACTIONS
        and quality.get("status") == "passed"
        and policy["decision"] not in {"avoid", "watch"}
    )

    recommendation_id = source_id or _make_id(code)
    links = signal_ledger.make_links(
        recommendation_id,
        correlation_id=correlation_id,
        signal_id=signal_id,
        trade_id=trade_id,
        monitor_id=monitor_id,
        include_trade=action in signal_ledger.TRADE_ACTIONS,
    )
    record = {
        "id": recommendation_id,
        **{key: value for key, value in links.items() if value is not None},
        "code": code,
        "name": name,
        "date": record_date,
        "created_at": datetime.now().isoformat(),
        "requested_action": action,
        "action": effective_action,
        "entry_price": entry_price,
        "price_range": price_range,
        "rationale": rationale,
        "risks": _clean_list(risks),
        "target_price": target_price,
        "stop_price": stop_price,
        "horizon": horizon,
        "grade": grade,
        "confidence": confidence,
        "strategy_id": strategy_id or "default",
        "position_sizing": sizing,
        "quality_report": quality,
        "policy_decision": policy,
        "portfolio_risk": risk,
        "research_evidence": evidence,
        "strategy_attributions": strategy_attributions(evidence),
        "execution_constraints": quality["execution_constraints"],
        "settleable_signal": opens_signal,
        "outcome": "pending",
    }
    ledger_events = [{
        "event_type": "recommendation.created",
        "links": links,
        "payload": record,
        "idempotency_key": f"recommendation.created:{recommendation_id}",
    }]
    if opens_signal:
        ledger_events.append(signal_ledger.signal_opened_event(record, links))
    if links.get("trade_id"):
        ledger_events.append({
            "event_type": "trade.proposed",
            "links": links,
            "payload": {
                "action": action,
                "effective_action": effective_action,
                "code": code,
                "entry_price": entry_price,
                "price_range": price_range,
                "strategy_id": record["strategy_id"],
                "status": "proposed",
                "execution_status": "not_executed",
                "quality_status": quality.get("status"),
                "policy_decision": policy,
            },
            "idempotency_key": f"trade.proposed:{links['trade_id']}",
        })
    signal_ledger.append_events(ledger_events, ledger_file=LEDGER_FILE)
    update_json_list(RECOMMENDATIONS_FILE, record, unique_key="id")
    return {"ok": True, "record": record}


def query_recommendations(code: Optional[str] = None, outcome: Optional[str] = None) -> List[Dict[str, Any]]:
    records = load_recommendations()
    if code:
        code = str(code).zfill(6)
        records = [r for r in records if r.get("code") == code]
    if outcome:
        records = [r for r in records if r.get("outcome") == outcome]
    return records


def update_outcome(rec_id: str, outcome: str, pnl_pct: Optional[float] = None, note: Optional[str] = None) -> Dict[str, Any]:
    outcome = outcome.lower().strip()
    if outcome not in VALID_OUTCOMES:
        return {"error": f"非法结果: {outcome}"}
    result: Dict[str, Any] = {"error": f"未找到推荐记录: {rec_id}"}
    existing = next((record for record in load_recommendations() if record.get("id") == rec_id), None)
    if not existing:
        return result
    opens_signal = existing.get("settleable_signal")
    if opens_signal is None:
        opens_signal = (
            existing.get("action") in signal_ledger.SETTLEABLE_ACTIONS
            and (existing.get("quality_report") or {}).get("status") == "passed"
        )
    if existing.get("signal_id") and opens_signal:
        mapped_outcome = {
            "profit": "win",
            "loss": "loss",
            "breakeven": "win",
            "invalidated": "invalidated",
            "pending": "pending",
        }[outcome]
        settlement = {
            "outcome": mapped_outcome,
            "pnl_pct": pnl_pct,
            "outcome_note": note,
        }
        signal_ledger.append_events(
            [
                signal_ledger.signal_opened_event(existing, signal_ledger.legacy_signal_links(existing)),
                signal_ledger.settlement_event(
                    existing,
                    settlement,
                    settlement_id=signal_ledger.make_settlement_id(
                        rec_id,
                        outcome,
                        pnl_pct,
                        note or "",
                    ),
                ),
            ],
            ledger_file=LEDGER_FILE,
        )

    def _mut(records: Any) -> List[Dict[str, Any]]:
        nonlocal result
        if not isinstance(records, list):
            records = []
        for record in records:
            if record.get("id") == rec_id:
                record["outcome"] = outcome
                record["resolved_at"] = datetime.now().isoformat()
                if pnl_pct is not None:
                    record["pnl_pct"] = pnl_pct
                if note:
                    record["outcome_note"] = note
                result = {"ok": True, "record": record}
                break
        return records

    mutate_json(RECOMMENDATIONS_FILE, _mut, [])
    return result


def example_record() -> Dict[str, Any]:
    return {
        "code": "002156",
        "name": "通富微电",
        "action": "buy",
        "price_range": "10.80-11.00",
        "rationale": "半导体主线明确，早盘强回封候选通过可成交性闸门",
        "risks": ["封板排队成交风险", "T+1低开需机械处置"],
        "target_price": 12.10,
        "stop_price": 10.45,
        "horizon": "T+1到T+3",
        "grade": "S",
        "confidence": "high",
        "strategy_id": "daban:first_board_reseal",
    }


def format_records(records: List[Dict[str, Any]]) -> str:
    if not records:
        return "📋 暂无推荐审计记录"
    lines = [
        "📋 **推荐审计记录**",
        f"共 {len(records)} 条",
        "",
        "| 日期 | 标的 | 动作 | 价格区间 | 等级 | 结果 |",
        "|------|------|------|----------|------|------|",
    ]
    for record in reversed(records[-20:]):
        lines.append(
            f"| {record.get('date', '?')} | {record.get('name')}({record.get('code')}) | "
            f"{record.get('action')} | {record.get('price_range')} | "
            f"{record.get('grade') or '-'} | {record.get('outcome')} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="推荐审计档案")
    parser.add_argument("--record", nargs=5, metavar=("CODE", "NAME", "ACTION", "PRICE_RANGE", "RATIONALE"))
    parser.add_argument("--risk", action="append", help="风险提示，可重复")
    parser.add_argument("--entry-price", type=float)
    parser.add_argument("--target-price", type=float)
    parser.add_argument("--stop-price", type=float)
    parser.add_argument("--horizon")
    parser.add_argument("--grade")
    parser.add_argument("--confidence")
    parser.add_argument("--strategy-id")
    parser.add_argument("--total-asset", type=float, default=100000.0)
    parser.add_argument("--list", action="store_true", help="列出推荐记录")
    parser.add_argument("--code", help="按代码过滤")
    parser.add_argument("--outcome", help="按结果过滤")
    parser.add_argument("--update", nargs=2, metavar=("REC_ID", "OUTCOME"), help="更新结果")
    parser.add_argument("--pnl", type=float, help="更新结果时记录收益率")
    parser.add_argument("--note", help="更新结果备注")
    parser.add_argument("--example", action="store_true", help="输出示例记录，不写入文件")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.example:
        output = {"schema": "recommendation_audit_example_v1", "record": example_record()}
    elif args.update:
        rec_id, outcome = args.update
        output = update_outcome(rec_id, outcome, args.pnl, args.note)
    elif args.record:
        code, name, action, price_range, rationale = args.record
        output = record_recommendation(
            code=code,
            name=name,
            action=action,
            price_range=price_range,
            rationale=rationale,
            risks=args.risk,
            entry_price=args.entry_price,
            target_price=args.target_price,
            stop_price=args.stop_price,
            horizon=args.horizon,
            grade=args.grade,
            confidence=args.confidence,
            strategy_id=args.strategy_id,
            total_asset=args.total_asset,
        )
    else:
        output = query_recommendations(args.code, args.outcome)

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(format_records(output if isinstance(output, list) else [output.get("record", output)]))
