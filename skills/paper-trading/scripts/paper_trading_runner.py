#!/usr/bin/env python3
"""Scheduled research-only paper broker; never sends real orders."""

from __future__ import annotations

import argparse
import json
import os
import site
from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


HERE = os.path.dirname(os.path.abspath(__file__))
COMMON = os.path.abspath(os.path.join(HERE, "..", "..", "common"))
site.addsitedir(COMMON)

from config_registry import load_registered  # noqa: E402
from market_adapters import fetch_tencent_snapshot  # noqa: E402
from paths import data_file  # noqa: E402
import paper_trading  # noqa: E402
import paper_trading_store as store  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402


SHANGHAI = ZoneInfo("Asia/Shanghai")
NAV_FILE = data_file("paper-trading", "paper_nav_latest.json")


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith(("sh", "sz", "bj")):
        text = text[2:]
    return text.zfill(6)


def _prefixed(code: str) -> str:
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def _normalize_quotes(quotes: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {_code(code): dict(quote) for code, quote in quotes.items()}


def _candidate_links(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    links = candidate.get("ledger_links") or candidate.get("links")
    return dict(links) if isinstance(links, Mapping) else None


def _append_rejection(
    candidate: Mapping[str, Any],
    gate: Mapping[str, Any],
    *,
    asof: str,
    observed_at: str,
    reason: str,
    config: Mapping[str, Any],
    **details: Any,
) -> None:
    code = _code(candidate.get("code"))
    store.append_paper_event(
        "paper.order.rejected",
        payload={
            "asof": asof,
            "observed_at": observed_at,
            "code": code,
            "reason": reason,
            **details,
            "gate": gate,
        },
        idempotency_key=f"paper.order.rejected:{asof}:{code}:{reason}",
        config=config,
        links=_candidate_links(candidate),
    )


def _process_open_candidate(
    account: Mapping[str, Any],
    candidate: Mapping[str, Any],
    quotes: Mapping[str, Mapping[str, Any]],
    *,
    asof: str,
    observed_at: str,
    config: Mapping[str, Any],
    risk: Mapping[str, Any],
    discipline: Mapping[str, Any],
    source_snapshot: Mapping[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    code = _code(candidate.get("code"))
    gate = paper_trading.evaluate_entry_gate(candidate, config)
    # reason 始终是本次的**最终**结论：门禁放行后若在下游被拒（缺行情、仓位算不出、
    # 纪律熔断…），要把真实原因覆盖上去，否则运行报告会显示 allowed=True 却
    # filled=0，自相矛盾且看不出该不该查（issue #174）。gate_reason 保留门禁结论。
    evaluation = {
        "code": code,
        "allowed": gate["allowed"],
        "reason": gate["reason"],
        "gate_reason": gate["reason"],
    }
    store.append_paper_event(
        "paper.candidate_evaluated",
        payload={
            "asof": asof,
            "observed_at": observed_at,
            "code": code,
            "name": candidate.get("name"),
            "open_score": candidate.get("open_score"),
            "recommendation_decision": candidate.get("decision"),
            "gate": gate,
            "source_snapshot": dict(source_snapshot),
        },
        idempotency_key=f"paper.candidate_evaluated:{asof}:{code}",
        config=config,
        links=_candidate_links(candidate),
    )
    if not gate["allowed"]:
        _append_rejection(
            candidate, gate, asof=asof, observed_at=observed_at,
            reason=str(gate["reason"]), config=config,
        )
        return dict(account), "rejected", evaluation
    trade_key = f"paper.trade.filled:{asof}:{code}:buy"
    if store.event_exists("paper.trade.filled", trade_key):
        return dict(account), "reused", evaluation
    if discipline.get("blocked"):
        _append_rejection(
            candidate, gate, asof=asof, observed_at=observed_at,
            reason="paper_discipline_blocked", config=config,
            discipline_state=dict(discipline),
        )
        evaluation["reason"] = "paper_discipline_blocked"
        return dict(account), "rejected", evaluation
    outcome = paper_trading.simulate_buy(
        account, candidate, quotes.get(code) or {}, asof=asof,
        observed_at=observed_at, config=config, risk=risk,
    )
    if outcome["status"] != "filled":
        _append_rejection(
            candidate, gate, asof=asof, observed_at=observed_at,
            reason=str(outcome["reason"]), config=config,
        )
        evaluation["reason"] = str(outcome["reason"])
        return dict(account), "rejected", evaluation
    updated = dict(outcome["account"])
    store.append_paper_event(
        "paper.trade.filled",
        payload={"trade": outcome["trade"], "gate": gate, "live_order_sent": False},
        idempotency_key=trade_key,
        config=config,
        account_after=updated,
        links=_candidate_links(candidate),
    )
    return updated, "filled", evaluation


def run_open(
    surface: Mapping[str, Any],
    quotes: Mapping[str, Mapping[str, Any]],
    *,
    asof: str,
    observed_at: str,
    config: Mapping[str, Any],
    risk: Mapping[str, Any],
    discipline_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    paper_trading.validate_open_surface(
        surface, asof=asof, observed_at=observed_at, config=config
    )
    account = store.load_account(config)
    account_key = f"paper.account.opened:{config['version']}"
    if not store.event_exists("paper.account.opened", account_key):
        store.append_paper_event(
            "paper.account.opened",
            payload={
                "opened_on": asof,
                "initial_cash": account.get("initial_cash"),
                "config_version": config["version"],
            },
            idempotency_key=account_key,
            config=config,
            account_after=account,
        )
    discipline = dict(discipline_state or {"blocked": False, "reasons": []})
    normalized_quotes = _normalize_quotes(quotes)
    filled = rejected = reused = 0
    evaluations = []
    candidates = sorted(
        [dict(item) for item in surface.get("signals") or []],
        key=lambda item: (-float(item.get("open_score") or 0), _code(item.get("code"))),
    )
    for candidate in candidates:
        account, result, evaluation = _process_open_candidate(
            account, candidate, normalized_quotes, asof=asof,
            observed_at=observed_at, config=config, risk=risk, discipline=discipline,
            source_snapshot=surface.get("input_snapshot") or {},
        )
        evaluations.append(evaluation)
        filled += result == "filled"
        rejected += result == "rejected"
        reused += result == "reused"
    # 0 成交时必须说清是「上游门禁按设计拒绝」还是「数据面缺了」——两者处置相反，
    # 而此前输出只有 filled=0/rejected=N，运维面上分不开（issue #174）。
    zero_fill = paper_trading.classify_zero_fill(
        evaluations,
        filled=filled,
        reused=reused,
    )
    # summarize_output 只保留 schema/status/message + 标量与列表计数，
    # zero_fill_class 是字符串会被丢掉；借 message 把它送到 cron 产物 summary 上，
    # 否则这次归因在运维面上看不见。
    message = (
        None if zero_fill["zero_fill_class"] is None
        else f"零成交归因: {zero_fill['zero_fill_class']}"
        + ("（需人工核查）" if zero_fill["actionable"] else "（设计内，无需处理）")
    )
    return {
        "schema": "paper_trading_open_run_v1",
        "status": "ok",
        "message": message,
        "asof": asof,
        "observed_at": observed_at,
        "evaluated": len(evaluations),
        "filled": filled,
        "rejected": rejected,
        "reused": reused,
        "zero_fill_class": zero_fill["zero_fill_class"],
        "zero_fill_actionable": zero_fill["actionable"],
        "zero_fill": zero_fill,
        "evaluations": evaluations,
        "cash": account["cash"],
        "position_count": len(account.get("positions") or []),
        "discipline_state": discipline,
        "research_only": True,
        "live_order_sent": False,
    }


def run_monitor(
    quotes: Mapping[str, Mapping[str, Any]],
    *,
    asof: str,
    observed_at: str,
    config: Mapping[str, Any],
    risk: Mapping[str, Any],
    time_stop_sessions: int,
) -> dict[str, Any]:
    account = store.load_account(config)
    if not account.get("positions"):
        return {"schema": "paper_trading_monitor_run_v1", "status": "no_positions", "asof": asof, "events": [], "research_only": True}
    result = paper_trading.simulate_exit_checks(
        account,
        _normalize_quotes(quotes),
        asof=asof,
        observed_at=observed_at,
        config=config,
        risk=risk,
        time_stop_sessions=time_stop_sessions,
    )
    latest_account = account
    for event in result["events"]:
        code = _code(event.get("code"))
        status = str(event.get("status") or "blocked")
        if status == "filled":
            event_type = "paper.trade.closed"
        elif status == "pending_t1":
            event_type = "paper.exit.pending_t1"
        elif status == "pending_unfilled":
            event_type = "paper.exit.unfilled"
        else:
            event_type = "paper.exit.check_blocked"
        snapshot = event.pop("account_after", None)
        if isinstance(snapshot, Mapping):
            latest_account = dict(snapshot)
        store.append_paper_event(
            event_type,
            payload={"asof": asof, "observed_at": observed_at, **event},
            idempotency_key=f"{event_type}:{asof}:{code}:{event.get('reason') or status}",
            config=config,
            account_after=latest_account if isinstance(snapshot, Mapping) else None,
        )
    store.append_paper_event(
        "paper.position.marked",
        payload={
            "asof": asof,
            "observed_at": observed_at,
            "position_count": len(result["account"].get("positions") or []),
        },
        idempotency_key=f"paper.position.marked:{asof}:{observed_at[11:16]}",
        config=config,
        account_after=result["account"],
    )
    return {
        "schema": "paper_trading_monitor_run_v1",
        "status": "ok",
        "asof": asof,
        "event_count": len(result["events"]),
        "events": result["events"],
        "cash": result["account"]["cash"],
        "position_count": len(result["account"].get("positions") or []),
        "research_only": True,
        "live_order_sent": False,
    }


def run_close(
    quotes: Mapping[str, Mapping[str, Any]],
    *,
    asof: str,
    observed_at: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    account = store.load_account(config)
    nav = paper_trading.mark_to_market(
        account, _normalize_quotes(quotes), asof=asof, observed_at=observed_at
    )
    store.append_paper_event(
        "paper.daily_nav",
        payload=nav,
        idempotency_key=f"paper.daily_nav:{asof}",
        config=config,
    )
    atomic_write_json(NAV_FILE, nav)
    return {"schema": "paper_trading_close_run_v1", **nav, "research_only": True}


def _fetch_for_codes(codes: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    return fetch_tencent_snapshot([_prefixed(_code(code)) for code in codes])


def _open_surface(asof: str) -> dict[str, Any]:
    path = data_file("daban-stock-picker", f"open_confirmation_{asof}.json")
    payload = read_json(path, None)
    if not isinstance(payload, Mapping):
        raise ValueError("open_confirmation_missing")
    return dict(payload)


def _locked_call(function, *args, **kwargs):
    with store.account_transaction():
        return function(*args, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description="研究专用模拟交易")
    parser.add_argument("--phase", required=True, choices=("open", "monitor", "close"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    now = datetime.now(SHANGHAI)
    asof = now.date().isoformat()
    observed_at = now.isoformat(timespec="seconds")
    config = load_registered("paper_trading")
    risk = load_registered("data_access")["risk"]
    time_stop = int(load_registered("daban_thresholds")["market_gate"]["position_time_stop_trading_days"])
    try:
        if args.phase == "open":
            surface = _open_surface(asof)
            allowed_codes = [
                _code(item.get("code"))
                for item in surface.get("signals") or []
                if paper_trading.evaluate_entry_gate(item, config)["allowed"]
            ]
            output = _locked_call(
                run_open,
                surface,
                _fetch_for_codes(allowed_codes),
                asof=asof,
                observed_at=observed_at,
                config=config,
                risk=risk,
                discipline_state=store.assess_paper_discipline(
                    asof=asof,
                    total_assets=paper_trading.portfolio_value(store.load_account(config)),
                    discipline_config=load_registered("daban_thresholds")["market_gate"],
                ),
            )
        else:
            account = store.load_account(config)
            quotes = _fetch_for_codes([item.get("code") for item in account.get("positions") or []])
            if args.phase == "monitor":
                output = _locked_call(
                    run_monitor,
                    quotes,
                    asof=asof,
                    observed_at=observed_at,
                    config=config,
                    risk=risk,
                    time_stop_sessions=time_stop,
                )
            else:
                output = _locked_call(
                    run_close,
                    quotes,
                    asof=asof,
                    observed_at=observed_at,
                    config=config,
                )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        output = {
            "schema": "paper_trading_run_v1",
            "status": "blocked",
            "phase": args.phase,
            "asof": asof,
            "reason": str(exc),
            "research_only": True,
            "live_order_sent": False,
        }
        code = 1
    else:
        code = 0
    print(json.dumps(output, ensure_ascii=False, indent=2 if args.json else None))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
