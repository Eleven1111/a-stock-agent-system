#!/usr/bin/env python3
"""Create simple bear/base/bull valuation scenarios."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def scenario(name: str, profit: float, multiple: float, current_market_cap: float | None) -> dict:
    market_cap = profit * multiple
    result = {
        "scenario": name,
        "net_profit": profit,
        "multiple": multiple,
        "implied_market_cap": round(market_cap, 4),
    }
    if current_market_cap and current_market_cap > 0:
        result["upside_downside_pct"] = round((market_cap / current_market_cap - 1) * 100, 2)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shares", type=float, help="shares outstanding in same unit as market cap calculation")
    parser.add_argument("--price", type=float, help="current price")
    parser.add_argument("--market-cap", type=float, help="current market cap; overrides shares*price")
    parser.add_argument("--currency", default="")
    parser.add_argument("--bear-profit", type=float, required=True)
    parser.add_argument("--bear-multiple", type=float, required=True)
    parser.add_argument("--base-profit", type=float, required=True)
    parser.add_argument("--base-multiple", type=float, required=True)
    parser.add_argument("--bull-profit", type=float, required=True)
    parser.add_argument("--bull-multiple", type=float, required=True)
    parser.add_argument("--unit", default="same unit for profit and market cap, e.g. CNY bn")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    current_market_cap = args.market_cap
    if current_market_cap is None and args.shares is not None and args.price is not None:
        current_market_cap = args.shares * args.price

    data = {
        "currency": args.currency,
        "unit": args.unit,
        "current_market_cap": current_market_cap,
        "input_warning": "Scenario math only. Analyst must verify forecast inputs, units, and multiples.",
        "scenarios": [
            scenario("bear", args.bear_profit, args.bear_multiple, current_market_cap),
            scenario("base", args.base_profit, args.base_multiple, current_market_cap),
            scenario("bull", args.bull_profit, args.bull_multiple, current_market_cap),
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
