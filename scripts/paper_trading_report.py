#!/usr/bin/env python3
"""Research report for the recommendation-then-Chanlun paper account."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import site
from typing import Any, Mapping, Sequence


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
site.addsitedir(COMMON)

from config_registry import load_registered  # noqa: E402
import signal_ledger  # noqa: E402


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _trade_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(event.get("payload") or {})
    return dict(payload.get("trade") or payload)


def _paper_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(event)
        for event in events
        if str(event.get("event_type") or "").startswith("paper.")
    ]


def _gate_summary(paper_events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    evaluations = [
        dict(event.get("payload") or {})
        for event in paper_events
        if event.get("event_type") == "paper.candidate_evaluated"
    ]
    passed = sum(bool((item.get("gate") or {}).get("allowed")) for item in evaluations)
    rejected_reasons = Counter(
        str((item.get("gate") or {}).get("reason") or "unknown")
        for item in evaluations
        if not (item.get("gate") or {}).get("allowed")
    )
    return {
        "evaluated": len(evaluations),
        "passed": passed,
        "rejected": len(evaluations) - passed,
        "pass_rate": round(passed / len(evaluations), 4) if evaluations else None,
        "rejection_reasons": dict(sorted(rejected_reasons.items())),
    }


def _execution_summary(paper_events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    buys = [
        _trade_payload(event)
        for event in paper_events
        if event.get("event_type") == "paper.trade.filled"
    ]
    closes = [
        _trade_payload(event)
        for event in paper_events
        if event.get("event_type") == "paper.trade.closed"
    ]
    pnls = [_number(item.get("realized_pnl")) for item in closes]
    wins = sum(value > 0 for value in pnls)
    return {
        "buys": len(buys),
        "closed_trades": len(closes),
        "wins": wins,
        "losses": sum(value < 0 for value in pnls),
        "win_rate": round(wins / len(pnls), 4) if pnls else None,
        "realized_pnl": round(sum(pnls), 2),
    }


def _account_summary(
    paper_events: Sequence[Mapping[str, Any]], initial_cash: float
) -> tuple[str, dict[str, Any]]:
    nav_rows = sorted(
        (
            dict(event.get("payload") or {})
            for event in paper_events
            if event.get("event_type") == "paper.daily_nav"
            and (event.get("payload") or {}).get("status") == "ok"
            and (event.get("payload") or {}).get("nav") is not None
        ),
        key=lambda item: str(item.get("asof") or ""),
    )
    navs = [_number(item.get("nav")) for item in nav_rows]
    peak = 0.0
    max_drawdown = 0.0
    for nav in navs:
        peak = max(peak, nav)
        if peak > 0:
            max_drawdown = min(max_drawdown, nav / peak - 1)
    final_nav = navs[-1] if navs else None
    total_return = (
        (final_nav / initial_cash - 1) * 100
        if final_nav is not None and initial_cash > 0
        else None
    )
    return "ready" if navs else "insufficient_data", {
        "initial_cash": round(initial_cash, 2),
        "nav_observations": len(navs),
        "final_nav": round(final_nav, 2) if final_nav is not None else None,
        "total_return_pct": round(total_return, 4) if total_return is not None else None,
        "max_drawdown_pct": round(max_drawdown * 100, 4) if navs else None,
    }


def build_report(
    events: Sequence[Mapping[str, Any]], *, initial_cash: float
) -> dict[str, Any]:
    paper_events = _paper_events(events)
    status, account = _account_summary(paper_events, initial_cash)
    return {
        "schema": "paper_trading_report_v1",
        "status": status,
        "strategy_contract": "open_recommendation_then_chanlun_filter",
        "candidate_gate": _gate_summary(paper_events),
        "execution": _execution_summary(paper_events),
        "account": account,
        "research_only": True,
        "live_policy_effect": "none",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟交易研究报告")
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    events = []
    for event in signal_ledger.read_events():
        payload = event.get("payload") or {}
        asof = str(payload.get("asof") or _trade_payload(event).get("trade_date") or "")
        if args.start and asof < args.start:
            continue
        if args.end and asof > args.end:
            continue
        events.append(event)
    config = load_registered("paper_trading")
    report = build_report(
        events,
        initial_cash=float(config["account"]["initial_cash"]),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
