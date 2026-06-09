#!/usr/bin/env python3
"""
Batch four-dimension scorer for scheduled cron runs.

The single-stock scorer still exists for manual use. Cron uses this wrapper so
the manifest is self-contained and does not need Gateway/agent-side template
variable injection.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Tuple

SCRIPT_DIR = os.path.dirname(__file__)
sys.path.insert(0, SCRIPT_DIR)

import four_dim_scorer  # noqa: E402


DEFAULT_TARGETS: List[Tuple[str, str]] = [
    ("600011", "华能国际"),
    ("600310", "广西能源"),
    ("002156", "通富微电"),
    ("600584", "长电科技"),
    ("002185", "华天科技"),
    ("000021", "深科技"),
    ("600667", "太极实业"),
]


def parse_targets(value: str | None) -> List[Tuple[str, str]]:
    if not value:
        return DEFAULT_TARGETS
    targets = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            code, name = item.split(":", 1)
        else:
            code, name = item, item
        targets.append((code.strip(), name.strip()))
    return targets


def score_targets(targets: List[Tuple[str, str]]) -> Dict[str, Any]:
    results = []
    for code, name in targets:
        try:
            result = four_dim_scorer.score_stock(code, name)
        except Exception as exc:
            result = {
                "code": code,
                "name": name,
                "status": "failed",
                "error": str(exc),
            }
        results.append(result)

    actionable = [
        r for r in results
        if r.get("confidence") in {"high", "medium"} and r.get("grade") in {"S", "A"}
    ]
    return {
        "schema": "four_dim_batch_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "ready",
        "target_count": len(results),
        "signals": actionable,
        "signal_count": len(actionable),
        "results": results,
    }


def format_report(batch: Dict[str, Any]) -> str:
    lines = [f"## 收盘四维批量打分 | {batch['target_count']}只"]
    for item in batch["results"]:
        if item.get("status") == "failed":
            lines.append(f"- {item['name']}({item['code']}): failed | {item['error']}")
            continue
        lines.append(
            f"- {item['name']}({item['code']}): {item.get('weighted')}/10 "
            f"{item.get('grade')} | {item.get('confidence')} | {item.get('advice')}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cron-safe batch four-dimension scorer")
    parser.add_argument("--targets", help="逗号分隔 code:name，如 002156:通富微电")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = score_targets(parse_targets(args.targets))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
