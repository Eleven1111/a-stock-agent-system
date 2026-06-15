#!/usr/bin/env python3
"""Collect independent A-share social attention rankings and publish snapshots."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Any, Callable, Mapping

SCRIPT_DIR = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

from market_snapshot import compact_ref, write_snapshot  # noqa: E402
from paths import data_file  # noqa: E402
from signal_context import update_signal_context  # noqa: E402
from social_attention import (  # noqa: E402
    build_social_attention_snapshot,
    write_social_attention_cache,
)
from social_attention_adapters import collect_social_rankings  # noqa: E402
from state_store import read_json  # noqa: E402


PRODUCER_VERSION = "social-attention-collector-v1"


def load_stock_metadata() -> dict[str, dict[str, Any]]:
    """Use only dynamic runtime state; never hardcode sectors or watchlists."""
    result: dict[str, dict[str, Any]] = {}
    for filename in ("exchange_universe.json", "candidate_pool_latest.json"):
        payload = read_json(data_file("stock-triage", filename), {})
        rows = (
            payload.get("stocks", [])
            if filename == "exchange_universe.json"
            else payload.get("candidates", [])
        )
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping) or not row.get("code"):
                continue
            code = str(row["code"]).lower()
            if code.startswith(("sh", "sz")):
                code = code[2:]
            code = code.zfill(6)
            existing = result.setdefault(code, {})
            for key in ("name", "sector", "industry"):
                if row.get(key):
                    existing[key] = row[key]
    return result


def _source_versions(source_health: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    versions = {}
    if source_health.get("eastmoney", {}).get("status") == "ok":
        versions["eastmoney_attention"] = "eastmoney-attention-v1"
    if source_health.get("xueqiu", {}).get("status") == "ok":
        versions["xueqiu_attention"] = "xueqiu-attention-v1"
    if source_health.get("baidu", {}).get("status") == "ok":
        versions["baidu_attention"] = "baidu-attention-v1"
    return versions


def run_collection(
    *,
    asof: str,
    batch_id: str,
    ranking_collector: Callable[
        [], tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]
    ] = collect_social_rankings,
    metadata_loader: Callable[[], Mapping[str, Mapping[str, Any]]] = load_stock_metadata,
) -> dict[str, Any]:
    rankings, source_health = ranking_collector()
    payload = build_social_attention_snapshot(
        rankings,
        trading_date=asof,
        source_health=source_health,
        stock_metadata=metadata_loader(),
    )
    snapshot = write_snapshot(
        "social-attention",
        payload,
        trading_date=asof,
        batch_id=batch_id,
        producer="social-attention",
        producer_version=PRODUCER_VERSION,
        source_versions=_source_versions(source_health),
        captured_at=payload["captured_at"],
    )
    snapshot_ref = compact_ref(snapshot)
    cache_updated = payload["status"] != "blocked"
    if cache_updated:
        write_social_attention_cache(payload, snapshot_ref)
        update_signal_context({
            "social_attention": payload,
            "social_attention_asof": asof,
            "social_attention_snapshot": snapshot_ref,
        })

    ordered = sorted(
        payload["stocks"].values(),
        key=lambda item: (
            not item["eligible_for_boost"],
            -item["attention_score"],
            item["code"],
        ),
    )
    return {
        "schema": "social_attention_collection_v1",
        "status": payload["status"],
        "asof": asof,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_health": source_health,
        "available_sources": payload["available_sources"],
        "stock_count": payload["stock_count"],
        "theme_count": payload["theme_count"],
        "top_stocks": ordered[:20],
        "top_themes": sorted(
            (
                {"theme": name, **details}
                for name, details in payload["themes"].items()
            ),
            key=lambda item: (-item["attention_score"], item["theme"]),
        )[:10],
        "snapshot_ref": snapshot_ref,
        "cache_updated": cache_updated,
    }


def format_report(result: Mapping[str, Any]) -> str:
    lines = [
        f"## A股社会关注度 | {result['asof']}",
        f"状态：{result['status']} | 来源：{', '.join(result['available_sources']) or '无'} "
        f"| 股票：{result['stock_count']} | 主题：{result['theme_count']}",
    ]
    for item in result.get("top_stocks", [])[:8]:
        lines.append(
            f"- {item['name']}({item['code']}): {item['attention_score']:.0f} "
            f"| {item['cross_source_count']}源 | 拥挤{item['crowding_risk']}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="A股社会关注度多源采集")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run_collection(
        asof=args.asof,
        batch_id=os.environ.get("A_STOCK_BATCH_ID")
        or f"a-share-{args.asof.replace('-', '')}",
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
