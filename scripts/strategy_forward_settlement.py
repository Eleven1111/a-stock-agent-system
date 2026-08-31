#!/usr/bin/env python3
"""Freeze Strategy Shadow predictions and settle due forward samples."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(ROOT)
import skills.common  # noqa: F401,E402

from paths import data_file  # noqa: E402
import strategy_forward_settlement as forward  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Strategy Shadow 前向样本冻结与结算")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--shadow")
    parser.add_argument(
        "--policy", default=os.path.join(ROOT, "config", "strategy_forward_settlement.json")
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    shadow = args.shadow or data_file(
        "stock-triage", os.path.join("strategy_shadow", f"{args.asof}.json")
    )
    result = forward.run(args.asof, shadow, policy_path=args.policy)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(
            f"strategy-forward-settlement {args.asof}: frozen={result['frozen']} "
            f"settled={result['settled']} pending={result['pending']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
