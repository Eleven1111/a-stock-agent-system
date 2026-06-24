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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Tuple

SCRIPT_DIR = os.path.dirname(__file__)
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "..", "common"))

import four_dim_scorer  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import read_json  # noqa: E402


Target = Mapping[str, Any] | Tuple[str, str]


def _target_code_name(target: Target) -> Tuple[str, str]:
    if isinstance(target, Mapping):
        code = str(target["code"])
        return code, str(target.get("name") or code)
    return target


def _target_strategy_id(target: Target) -> str:
    if not isinstance(target, Mapping):
        return "four_dim"
    explicit = str(target.get("strategy_id") or "")
    if explicit:
        return explicit
    for key in ("open_selected_by", "auction_selected_by", "selected_by"):
        selected = target.get(key)
        if isinstance(selected, Mapping):
            if selected.get("daban"):
                return "daban:first_board_reseal"
            if selected.get("trend"):
                return "trend_pullback"
    return "four_dim"


def _target_sector(target: Target) -> str | None:
    return str(target.get("sector")) if isinstance(target, Mapping) and target.get("sector") else None


def load_pool_targets(limit: int = 20, asof: str | None = None) -> List[Dict[str, Any]]:
    pool = read_json(data_file("stock-triage", "candidate_pool_latest.json"), {})
    expected_asof = asof or date.today().isoformat()
    if (
        not isinstance(pool, dict)
        or pool.get("status") != "ready"
        or pool.get("asof") != expected_asof
    ):
        return []
    return [dict(item) for item in pool.get("candidates", [])[:limit] if item.get("code")]


def parse_targets(
    value: str | None,
    limit: int = 20,
    asof: str | None = None,
) -> List[Target]:
    if not value:
        return load_pool_targets(limit, asof=asof)
    targets = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            code, name = item.split(":", 1)
        else:
            code, name = item, item
        targets.append({"code": code.strip(), "name": name.strip(), "strategy_id": "four_dim"})
    return targets


def _market_of(code: str) -> str:
    return "sz" if code.startswith(("0", "3")) else "sh"


def _prefetch_quotes(targets: List[Target]) -> Dict[str, Any]:
    """一次性批量抓全部标的实时行情（腾讯支持多代码），省去每票各抓一次。"""
    try:
        from a_stock_http import fetch_tencent_quote
        codes = [f"{_market_of(c)}{c}" for c, _ in (_target_code_name(t) for t in targets)]
        return fetch_tencent_quote(codes) or {}
    except Exception:  # noqa: BLE001
        return {}


def score_targets(targets: List[Target], max_workers: int = 5) -> Dict[str, Any]:
    if not targets:
        return {
            "schema": "four_dim_batch_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "insufficient_data",
            "error": "动态候选池缺失或为空",
            "target_count": 0,
            "signals": [],
            "signal_count": 0,
            "results": [],
        }
    quote_map = _prefetch_quotes(targets)

    def _one(target: Target) -> Dict[str, Any]:
        code, name = _target_code_name(target)
        q = quote_map.get(f"{_market_of(code)}{code}")
        if not (isinstance(q, dict) and q.get("price") is not None):
            q = None  # 预取未命中/不完整 → 让 score_stock 自抓，保留原 error 处理
        try:
            return four_dim_scorer.score_stock(
                code,
                name,
                quote=q,
                strategy_id=_target_strategy_id(target),
                sector=_target_sector(target),
            )
        except Exception as exc:  # noqa: BLE001
            return {"code": code, "name": name, "status": "failed", "error": str(exc)}

    workers = min(max_workers, len(targets)) or 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_one, targets))  # map 保持 targets 顺序

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
    parser.add_argument("--targets", help="逗号分隔 code:name，如 600519:贵州茅台")
    parser.add_argument("--limit", type=int, default=20, help="从动态候选池读取的标的上限")
    parser.add_argument("--asof", default=date.today().isoformat(), help="动态候选池交易日")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = score_targets(parse_targets(args.targets, limit=args.limit, asof=args.asof))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
