#!/usr/bin/env python3
"""Run S1/S3/S5 daily-bar exploratory baselines against the local cache."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from collections.abc import Iterator
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(os.fspath(ROOT))
import skills.common  # noqa: F401,E402
import exploratory_strategy_baseline as baseline  # noqa: E402


DEFAULT_POLICY = ROOT / "config" / "exploratory_strategy_baseline.json"
DEFAULT_DB = Path("~/.hermes/market/history.sqlite3").expanduser()


def load_bars(database: str | Path, *, adjust_flag: str = "qfq") -> Iterator[dict[str, Any]]:
    path = Path(database).expanduser().resolve()
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT code, trading_date, open, high, low, close, volume,
                      amount, turn, source, source_version, updated_at
               FROM daily_bars WHERE adjust_flag = ?
               ORDER BY code, trading_date""",
            (adjust_flag,),
        )
        for row in rows:
            yield dict(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="S1/S3/S5 日线可观测子假设 walk-forward（仅 exploratory，不进 research gate）")
    parser.add_argument("--database", default=str(DEFAULT_DB), help="本地 history.sqlite3（只读打开）")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="exploratory policy JSON")
    parser.add_argument("--output", default=None, help="可选报告 JSON；默认只打印，不写生产状态")
    args = parser.parse_args()
    policy = baseline.load_policy(args.policy)
    report = baseline.run(load_bars(args.database, adjust_flag=policy["adjust_flag"]), policy)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        target = Path(args.output).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
