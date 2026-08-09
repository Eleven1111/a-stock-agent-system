#!/usr/bin/env python3
"""
每日执行纪律复盘 — 建议 vs 实际成交 + 尚未处理的持仓纪律信号
============================================================
不判断对错：跟单与否可能是正确的临场决策。只把"当天建议是什么、实际做了什么、
差在哪"摆出来，把复盘从记忆里的模糊印象变成一份可核对的清单。

Usage:
  python3 discipline_review.py                    # 今日复盘 + 实时行情刷新
  python3 discipline_review.py --asof 2026-06-24   # 指定交易日
  python3 discipline_review.py --no-refresh --json # 离线/测试：不刷新持仓行情
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
from portfolio_policy import portfolio_value  # noqa: E402
import recommendation_audit  # noqa: E402
import signal_ledger  # noqa: E402
import portfolio_manager  # noqa: E402
import trading_discipline  # noqa: E402

SCHEMA = "discipline_review_v1"
CHASE_TOLERANCE_PCT = 1.0  # 允许略高于建议区间上沿的挂单误差（滑点/摩擦）
OVERSIZED_MULTIPLIER = 1.3  # 实际仓位超过建议仓位的这个倍数才标记


def _parse_price_range(price_range: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    if not price_range:
        return None, None
    parts = str(price_range).replace("N/A", "").split("-")
    try:
        if len(parts) == 2 and parts[0] and parts[1]:
            return float(parts[0]), float(parts[1])
        if len(parts) == 1 and parts[0]:
            value = float(parts[0])
            return value, value
    except ValueError:
        pass
    return None, None


def _trade_events_for_date(events: Sequence[Mapping[str, Any]], asof: str) -> List[Dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in events
        if str(event.get("event_type")) == "trade.executed"
        and (event.get("payload") or {}).get("trade_date") == asof
    ]


def review_buy_side(
    recommendations: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    *,
    asof: str,
    total_assets: float,
) -> List[Dict[str, Any]]:
    """逐条比对当日 buy/add 建议 vs 实际成交：是否跟单、追价、超仓位。"""
    opens_by_code: Dict[str, List[Dict[str, Any]]] = {}
    for trade in trades:
        if str(trade.get("action")) in {"open", "add"}:
            opens_by_code.setdefault(str(trade.get("code") or "").zfill(6), []).append(trade)

    rows: List[Dict[str, Any]] = []
    for rec in recommendations:
        if rec.get("date") != asof or str(rec.get("action")) not in {"buy", "add"}:
            continue
        code = str(rec.get("code") or "").zfill(6)
        matches = opens_by_code.get(code) or []
        recommended_pct = (rec.get("position_sizing") or {}).get("recommended_position_pct")
        row: Dict[str, Any] = {
            "code": code,
            "name": rec.get("name"),
            "recommended_action": rec.get("action"),
            "recommended_price_range": rec.get("price_range"),
            "recommended_position_pct": recommended_pct,
            "followed": bool(matches),
            "flags": [],
        }
        if not matches:
            row["flags"].append("未跟单")
            rows.append(row)
            continue

        trade = matches[0]
        price = trade.get("price")
        shares = trade.get("shares")
        row["executed_price"] = price
        row["executed_shares"] = shares

        _, high = _parse_price_range(rec.get("price_range"))
        if high is not None and price is not None and float(price) > high * (1 + CHASE_TOLERANCE_PCT / 100):
            row["flags"].append(f"追价：成交{price} 高于建议上沿{high}")

        if total_assets > 0 and price is not None and shares is not None:
            actual_pct = round(float(price) * float(shares) / total_assets * 100, 2)
            row["actual_position_pct"] = actual_pct
            if recommended_pct and actual_pct > float(recommended_pct) * OVERSIZED_MULTIPLIER:
                row["flags"].append(f"超仓位：实际{actual_pct}% 高于建议{recommended_pct}%")
        rows.append(row)
    return rows


def build_review(asof: Optional[str] = None, *, refresh_prices: bool = False) -> Dict[str, Any]:
    """asof 为空取今日；refresh_prices=True 才会触网刷新持仓现价（离线/测试默认关闭）。"""
    asof = asof or date.today().isoformat()
    recommendations = recommendation_audit.load_recommendations()
    events = signal_ledger.read_events()
    trades = _trade_events_for_date(events, asof)
    portfolio = portfolio_manager.load_portfolio()
    total_assets = portfolio_value(portfolio)

    pending_alerts: List[Dict[str, Any]] = []
    if refresh_prices:
        _, price_result = portfolio_manager.refresh_prices()
        pending_alerts = list(price_result.get("alerts") or [])

    discipline_state = trading_discipline.assess_discipline_state(
        events, total_assets=total_assets, asof=asof,
    )

    return {
        "schema": SCHEMA,
        "asof": asof,
        "buy_side": review_buy_side(recommendations, trades, asof=asof, total_assets=total_assets),
        "pending_exit_signals": pending_alerts,
        "discipline_state": discipline_state,
    }


def format_report(review: Mapping[str, Any]) -> str:
    lines = [f"## 执行纪律复盘 | {review['asof']}"]
    buy_side = review.get("buy_side") or []
    if not buy_side:
        lines.append("- 当日无买入类建议")
    for row in buy_side:
        flag_text = "；".join(row["flags"]) if row["flags"] else "无异常"
        lines.append(
            f"- {row.get('name')}({row['code']}): 建议{row['recommended_action']} "
            f"区间={row.get('recommended_price_range')} | "
            f"{'已跟单' if row['followed'] else '未跟单'} | {flag_text}"
        )
    alerts = review.get("pending_exit_signals") or []
    if alerts:
        lines.append("\n### 尚未处理的持仓纪律信号")
        for alert in alerts:
            lines.append(f"- {alert.get('level')} {alert.get('msg')}")
    discipline = review.get("discipline_state") or {}
    if discipline.get("blocked"):
        lines.append(f"\n账户纪律熔断中：{'；'.join(discipline.get('reasons') or [])}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="每日执行纪律复盘")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--no-refresh", action="store_true", help="跳过实时行情刷新（离线/测试用）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    review = build_review(args.asof, refresh_prices=not args.no_refresh)
    if args.json:
        print(json.dumps(review, ensure_ascii=False))
    else:
        print(format_report(review))


if __name__ == "__main__":
    main()
