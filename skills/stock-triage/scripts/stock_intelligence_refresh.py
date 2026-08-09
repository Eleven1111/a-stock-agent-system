#!/usr/bin/env python3
"""Refresh source-versioned stock intelligence for holdings and top candidates."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
from typing import Any

COMMON_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "common")
)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from eastmoney_intelligence import ADAPTER_VERSION, provider_health  # noqa: E402
from market_snapshot import compact_ref, write_snapshot  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import read_json  # noqa: E402
import monitor_registry  # noqa: E402
import runtime_targets  # noqa: E402
import stock_intelligence  # noqa: E402


def _code(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith(("sh", "sz", "bj")):
        raw = raw[2:]
    return raw[-6:].zfill(6)


def build_targets(
    *,
    portfolio: dict[str, Any],
    candidate_pool: dict[str, Any],
    registry: list[dict[str, Any]] | None = None,
    candidate_limit: int = 5,
) -> list[dict[str, str]]:
    return runtime_targets.build_stock_targets(
        portfolio=portfolio,
        registry=registry,
        candidate_pool=candidate_pool,
        candidate_limit=candidate_limit,
    )


def load_targets(candidate_limit: int = 5) -> list[dict[str, str]]:
    return build_targets(
        portfolio=read_json(
            data_file("stock-triage", "portfolio.json"),
            {"positions": []},
        ),
        candidate_pool=read_json(
            data_file("stock-triage", "candidate_pool_latest.json"),
            {"candidates": []},
        ),
        registry=monitor_registry.load_registry(),
        candidate_limit=candidate_limit,
    )


def refresh(
    targets: list[dict[str, str]],
    *,
    asof: str,
) -> dict[str, Any]:
    batch_id = (
        os.environ.get("A_STOCK_BATCH_ID")
        or f"a-share-{asof.replace('-', '')}"
    )
    results = []
    for target in targets:
        payload = stock_intelligence.collect(
            target["code"],
            name=target["name"],
            asof=asof,
        )
        snapshot = write_snapshot(
            "stock-intelligence",
            payload,
            trading_date=asof,
            batch_id=batch_id,
            producer="stock-intelligence-refresh",
            producer_version="v2",
            source_versions={"eastmoney": ADAPTER_VERSION},
        )
        payload["snapshot_ref"] = compact_ref(snapshot)
        stock_intelligence.write_cache(payload)
        results.append({
            "code": target["code"],
            "name": target["name"],
            "source": target["source"],
            "data_quality": payload["data_quality"],
            "risk_summary": payload["risk_summary"],
            "provider_health": (payload.get("source") or {}).get("health"),
            "snapshot_ref": payload["snapshot_ref"],
        })
    partial = [
        item for item in results
        if item["data_quality"].get("status") != "complete"
    ]
    return {
        "schema": "stock_intelligence_refresh_v1",
        "asof": asof,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "partial" if partial else "ready",
        "target_count": len(targets),
        "partial_count": len(partial),
        "provider_health": provider_health(),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新筹码与机构证据")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--candidate-limit", type=int, default=5)
    parser.add_argument("--codes", help="逗号分隔，可用 code:name")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.codes:
        targets = []
        for raw in args.codes.split(","):
            code, _, name = raw.strip().partition(":")
            normalized = _code(code)
            if normalized.strip("0"):
                targets.append({
                    "code": normalized,
                    "name": name or normalized,
                    "source": "explicit",
                })
    else:
        targets = load_targets(candidate_limit=args.candidate_limit)
    result = refresh(targets, asof=args.asof)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"筹码/机构证据刷新: {result['target_count']}只, "
            f"部分失败{result['partial_count']}只"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
