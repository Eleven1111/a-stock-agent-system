#!/usr/bin/env python3
"""Precompute same-day portfolio admission evidence from prior daily bars."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from datetime import date
from typing import Any, Mapping

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(ROOT)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from a_stock_http import DataSourceError  # noqa: E402
from market_adapters import fetch_tencent_kline  # noqa: E402
from market_snapshot import compact_ref, materialize_input_snapshot  # noqa: E402
from paths import data_file  # noqa: E402
from portfolio_risk_evidence import build_evidence_bundle, normalize_code  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402


MAX_CANDIDATES = 20
MAX_HOLDINGS = 20
LOOKBACK_DAYS = 80
BENCHMARK_CODE = "000300"


def output_path(asof: str) -> str:
    return data_file("stock-triage", f"portfolio_risk_evidence_{asof}.json")


def latest_output_path() -> str:
    return data_file("stock-triage", "portfolio_risk_evidence_latest.json")


def shortlist_path(asof: str) -> str:
    return data_file("daban-stock-picker", f"auction_shortlist_{asof}.json")


def _market(code: str) -> str:
    return "sh" if normalize_code(code).startswith(("5", "6", "9")) else "sz"


def _candidate_codes(candidates: list[Mapping[str, Any]]) -> list[str]:
    return list(dict.fromkeys(
        normalize_code(item.get("code"))
        for item in candidates
        if normalize_code(item.get("code")) != "000000"
    ))


def build_batch(asof: str) -> dict[str, Any]:
    shortlist = read_json(shortlist_path(asof), {})
    if not isinstance(shortlist, dict) or str(shortlist.get("asof") or "") != asof:
        raise DataSourceError("auction_shortlist", f"{asof} 竞价短名单缺失")
    candidates = [
        dict(item) for item in (shortlist.get("shortlist") or [])[:MAX_CANDIDATES]
        if isinstance(item, Mapping)
    ]
    portfolio = read_json(data_file("stock-triage", "portfolio.json"), {})
    holding_codes = [
        normalize_code(item.get("code"))
        for item in (portfolio.get("positions") or [])
        if isinstance(item, Mapping)
    ][:MAX_HOLDINGS]
    codes = list(dict.fromkeys([*_candidate_codes(candidates), *holding_codes]))
    def fetch_history(
        key: str, code: str, market: str
    ) -> tuple[str, list[dict[str, Any]]]:
        try:
            rows = fetch_tencent_kline(
                code,
                market=market,
                days=LOOKBACK_DAYS,
                ktype="day",
            )
        except DataSourceError:
            rows = []
        return key, rows

    requests = [(code, code, _market(code)) for code in codes]
    requests.append(("__benchmark__", BENCHMARK_CODE, "sh"))
    with ThreadPoolExecutor(max_workers=min(4, len(requests))) as executor:
        histories = dict(executor.map(lambda item: fetch_history(*item), requests))
    benchmark_bars = histories.pop("__benchmark__", [])
    bars_by_code = histories
    batch_id = os.environ.get("A_STOCK_BATCH_ID") or f"a-share-{asof.replace('-', '')}"
    input_snapshot = materialize_input_snapshot(
        "portfolio-risk-precompute-input",
        {
            "schema": "portfolio_risk_precompute_inputs_v1",
            "candidates": candidates,
            "portfolio": portfolio,
            "bars_by_code": bars_by_code,
            "benchmark_bars": benchmark_bars,
        },
        trading_date=asof,
        batch_id=batch_id,
        producer="portfolio-risk-precompute",
        producer_version="portfolio-risk-evidence-v2",
        source_versions={"tencent": "tencent-adapter-v3"},
    )
    payload = input_snapshot["payload"]
    result = build_evidence_bundle(
        list(payload.get("candidates") or []),
        dict(payload.get("portfolio") or {}),
        bars_by_code=dict(payload.get("bars_by_code") or {}),
        benchmark_bars=list(payload.get("benchmark_bars") or []),
        proposed_position_pct=4.0,
        decision_asof=asof,
    )
    evidence = list(result["evidence_by_code"].values())
    fully_covered = all(float(item.get("coverage") or 0) >= 0.95 for item in evidence)
    result.update({
        "status": "ready" if fully_covered and str(shortlist.get("status")) != "degraded" else "degraded",
        "candidate_count": len(candidates),
        "complete_count": sum(float(item.get("coverage") or 0) >= 0.95 for item in evidence),
        "batch_id": batch_id,
        "input_snapshot": compact_ref(input_snapshot),
        "source_versions": {"tencent": "tencent-adapter-v3"},
    })
    atomic_write_json(output_path(asof), result)
    atomic_write_json(latest_output_path(), result)
    return result


def json_report(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": result.get("schema"),
        "status": result.get("status"),
        "asof": result.get("asof"),
        "candidate_count": result.get("candidate_count"),
        "complete_count": result.get("complete_count"),
        "input_snapshot": result.get("input_snapshot"),
    }


def _require_same_day_live(asof: str) -> None:
    if str(asof)[:10] != date.today().isoformat():
        raise DataSourceError(
            "portfolio_risk_precompute",
            "historical --asof requires immutable portfolio and market replay inputs",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="盘前组合因子与流动性证据预计算")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        _require_same_day_live(args.asof)
        result = build_batch(args.asof)
    except DataSourceError as exc:
        result = {
            "schema": "portfolio_risk_evidence_batch_v1",
            "status": "insufficient_data",
            "asof": args.asof,
            "error": str(exc),
        }
    print(json.dumps(json_report(result) if args.json else result, ensure_ascii=False))
    return 0 if result.get("status") in {"ready", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
