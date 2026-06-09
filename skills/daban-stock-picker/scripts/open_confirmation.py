#!/usr/bin/env python3
"""
09:35 open confirmation for the limit-up candidate workflow.

Reads the 09:25 auction factor state, fetches current Tencent quotes, and emits
a compact JSON decision surface for stock-triage. It does not place orders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Any, Dict, List

SCRIPT_DIR = os.path.dirname(__file__)
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "..", "common"))

import auction_collector  # noqa: E402
from a_stock_http import DataSourceError, fetch_tencent_snapshot  # noqa: E402
from tradeability import assess_tradeability  # noqa: E402


def _naked_code(code: str) -> str:
    return code[2:] if code.startswith(("sh", "sz")) else code


def evaluate_open_confirmation(factor: Dict[str, Any], quote: Dict[str, Any]) -> Dict[str, Any]:
    code = factor.get("code", "")
    name = factor.get("name") or quote.get("name") or code
    tradeability = assess_tradeability(quote, _naked_code(code), name)
    change_pct = quote.get("change_pct")
    price = quote.get("price")

    action = "skip"
    reasons: List[str] = []
    if factor.get("error"):
        reasons.append(factor["error"])
    elif tradeability.get("tradeable") is False:
        action = "not_buyable"
        reasons.append(tradeability.get("reason", "不可成交"))
    elif factor.get("is_yiziban"):
        action = "not_buyable"
        reasons.append("09:25一字封死，高分也可能打不进")
    elif tradeability.get("status") == "limit_up":
        action = "queue_or_skip"
        reasons.append("已封涨停，仅可排队且不保证成交")
    elif change_pct is not None and 3.0 <= change_pct < 9.5:
        action = "trend_watch"
        reasons.append("符合用户偏好的3%-10%中度上涨观察窗口")
    elif factor.get("board_status") in {"high_open", "limit_up_with_ask"}:
        action = "watch"
        reasons.append("竞价强但开盘未形成明确可执行信号")
    else:
        reasons.append("开盘确认不足")

    return {
        "code": code,
        "name": name,
        "price": price,
        "change_pct": change_pct,
        "auction_gap_pct": factor.get("auction_gap_pct"),
        "board_status": factor.get("board_status"),
        "action": action,
        "tradeability": tradeability,
        "reasons": reasons,
    }


def build_confirmation(codes: List[str], asof: str) -> Dict[str, Any]:
    factors = auction_collector.finalize(asof).get("factors", [])
    if codes:
        wanted = set(codes)
        factors = [f for f in factors if f.get("code") in wanted]
    quote_codes = [f["code"] for f in factors if f.get("code") and not f.get("error")]
    quotes = fetch_tencent_snapshot(quote_codes) if quote_codes else {}

    confirmations = []
    for factor in factors:
        quote = quotes.get(factor.get("code"), {})
        confirmations.append(evaluate_open_confirmation(factor, quote))

    actionable = [c for c in confirmations if c["action"] not in {"skip", "not_buyable"}]
    return {
        "schema": "open_confirmation_v1",
        "asof": asof,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "ready",
        "confirmations": confirmations,
        "signals": actionable,
        "signal_count": len(actionable),
    }


def format_report(result: Dict[str, Any]) -> str:
    if not result["confirmations"]:
        return "09:35 开盘确认：无竞价候选"
    lines = [f"## 09:35 开盘确认 | {result['asof']}"]
    for item in result["confirmations"]:
        lines.append(
            f"- {item['name']}({item['code']}): {item['action']} "
            f"现价={item.get('price')} 涨幅={item.get('change_pct')}% "
            f"竞价={item.get('auction_gap_pct')}% | {'；'.join(item['reasons'])}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="A股09:35开盘确认")
    parser.add_argument("--codes", help="逗号分隔，带市场前缀，如 sh600011,sz002156")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else []
    try:
        result = build_confirmation(codes, args.asof)
    except DataSourceError as exc:
        result = {
            "schema": "open_confirmation_v1",
            "asof": args.asof,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "insufficient_data",
            "error": str(exc),
            "confirmations": [],
            "signals": [],
            "signal_count": 0,
        }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
