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
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Tuple

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import four_dim_scorer  # noqa: E402
import four_dim_score_log  # noqa: E402
import local_market_history  # noqa: E402
from paths import data_file  # noqa: E402


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


def _dated_pool_path(asof: str) -> str:
    return data_file("stock-triage", os.path.join("candidate_pools", f"{asof}.json"))


def _supports_lane(target: Mapping[str, Any], lane: str) -> bool:
    for key in ("open_selected_by", "auction_selected_by", "selected_by"):
        selected = target.get(key)
        if isinstance(selected, Mapping) and selected.get(lane):
            return True
    return isinstance(target.get(f"{lane}_score"), (int, float))


def _lane_targets(candidates: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    quotas = {"trend": (limit + 1) // 2, "daban": limit // 2}
    expanded: List[Dict[str, Any]] = []
    for lane in ("trend", "daban"):
        eligible = [item for item in candidates if _supports_lane(item, lane)]
        eligible.sort(key=lambda item: (item.get(f"{lane}_rank") is None, item.get(f"{lane}_rank", 10**9)))
        strategy_id = "trend_pullback" if lane == "trend" else "daban:first_board_reseal"
        for item in eligible[:quotas[lane]]:
            expanded.append({**item, "strategy_id": strategy_id, "research_lane": lane})
    return expanded


def load_pool_targets(limit: int = 20, asof: str | None = None) -> List[Dict[str, Any]]:
    expected_asof = asof or date.today().isoformat()
    try:
        with open(_dated_pool_path(expected_asof), encoding="utf-8") as handle:
            pool = json.load(handle)
    except (OSError, json.JSONDecodeError):
        pool = {}
    if (
        not isinstance(pool, dict)
        or pool.get("status") != "ready"
        or pool.get("asof") != expected_asof
    ):
        return []
    candidates = [
        {**item, "snapshot_asof": pool["asof"]}
        for item in pool.get("candidates", []) if item.get("code")
    ]
    return _lane_targets(candidates, max(0, limit))


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


def _cached_quote(target: Target, asof: str) -> Dict[str, Any]:
    if not isinstance(target, Mapping):
        return {"provider": "cache_only_missing_snapshot_fields"}
    quote = dict(target)
    quote_asof = target.get("asof") or target.get("fetched_at") or target.get("snapshot_asof")
    if quote_asof:
        quote["asof"] = str(quote_asof)
    else:
        quote.pop("asof", None)
    quote["provider"] = str(target.get("provider") or target.get("quote_source") or "candidate_pool_snapshot")
    return quote


def _cached_klines(code: str, asof: str) -> List[Dict[str, Any]]:
    try:
        rows = local_market_history.get_daily_bars([code], asof, 60, adjust_flag="qfq")
    except (OSError, sqlite3.Error, ValueError):
        return []
    return [{**row, "date": str(row.get("trading_date") or row.get("date") or "")} for row in rows]


def score_targets(
    targets: List[Target], max_workers: int = 5, *, cache_only: bool = False, asof: str | None = None,
) -> Dict[str, Any]:
    event_asof = str(asof or date.today().isoformat())
    if not targets:
        return {
            "schema": "four_dim_batch_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "insufficient_data",
            "error": "动态候选池缺失或为空",
            "target_count": 0,
            "signals": [],
            "signal_count": 0,
            "research_candidates": [],
            "research_candidate_count": 0,
            "results": [],
            "cache_only": cache_only,
            "research_only": True,
            "live_effect": "none",
        }
    quote_map = {} if cache_only else _prefetch_quotes(targets)

    def _one(target: Target) -> Dict[str, Any]:
        code, name = _target_code_name(target)
        q = _cached_quote(target, event_asof) if cache_only else quote_map.get(f"{_market_of(code)}{code}")
        if not cache_only and not (isinstance(q, dict) and q.get("price") is not None):
            q = None  # 预取未命中/不完整 → 让 score_stock 自抓，保留原 error 处理
        klines = _cached_klines(code, event_asof) if cache_only else None
        try:
            return four_dim_scorer.score_stock(
                code,
                name,
                quote=q,
                klines=klines,
                strategy_id=_target_strategy_id(target),
                sector=_target_sector(target),
                asof=event_asof,
                cache_only=cache_only,
            )
        except Exception as exc:  # noqa: BLE001
            return {"code": code, "name": name, "status": "failed", "error": str(exc)}

    workers = min(max_workers, len(targets)) or 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_one, targets))  # map 保持 targets 顺序

    research_candidates = []
    for raw in results:
        if raw.get("confidence") not in {"high", "medium"} or raw.get("grade") not in {"S", "A"}:
            continue
        item = dict(raw)
        item.update({
            "directional_ready": False,
            "execution_action": "none",
            "policy_status": "not_evaluated",
        })
        research_candidates.append(item)
    return {
        "schema": "four_dim_batch_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "ready",
        "target_count": len(results),
        # Raw factor scores never become directional cron signals. Downstream
        # policy must produce a separate, fully gated recommendation artifact.
        "signals": [],
        "signal_count": 0,
        "research_candidates": research_candidates,
        "research_candidate_count": len(research_candidates),
        "results": results,
        "cache_only": cache_only,
        "research_only": True,
        "live_effect": "none",
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
    parser.add_argument("--cache-only", action="store_true", help="只读候选快照、本地日线和研究缓存")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = score_targets(
        parse_targets(args.targets, limit=args.limit, asof=args.asof),
        cache_only=args.cache_only,
        asof=args.asof,
    )
    # Instrumentation (T5): persist four_dim sub-scores keyed by (code, asof) so
    # they can later be joined with settled candidate_lifecycle outcomes. Never
    # blocks the scorer's own output.
    snapshot_path = None if args.targets else _dated_pool_path(args.asof)
    four_dim_score_log.record_scores(result, asof=args.asof, input_snapshot_path=snapshot_path)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
