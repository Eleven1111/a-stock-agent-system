#!/usr/bin/env python3
"""CLI for exploratory point-in-time Four-Dimension technical replay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import skills.common  # noqa: F401,E402
from paths import data_file  # noqa: E402
import four_dim_pit_replay  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Four-Dimension technical-only PIT replay")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--codes", help="comma-separated six-digit codes; omitted means cached daily universe")
    parser.add_argument(
        "--policy",
        default=str(ROOT / "config" / "four_dim_pit_replay.json"),
    )
    parser.add_argument(
        "--output-root",
        default=data_file("stock-triage", "research/four_dim_pit_replay"),
    )
    args = parser.parse_args()
    codes = [item.strip() for item in (args.codes or "").split(",") if item.strip()] or None
    result = four_dim_pit_replay.run(
        start_date=args.start_date,
        end_date=args.end_date,
        policy_path=args.policy,
        output_root=args.output_root,
        codes=codes,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))


if __name__ == "__main__":
    main()
