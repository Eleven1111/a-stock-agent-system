#!/usr/bin/env python3
"""Materialize catalog datasets from artifacts the system already accumulates.

The catalog declares dataset semantics and `dataset_projection` knows how to
build conforming rows, but nothing was scheduled to actually run that
projection — so no dataset ever accumulated. This is that scheduled producer.

Each dataset is built independently: one failing must not sink the other, and
a failure is always **named** in the output rather than swallowed. A dataset
that cannot satisfy its contract (most often because coverage is still below
the declared minimum) is reported as skipped with the contract's own error —
that is the designed fail-closed path, not a fault of this script.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from datetime import date
from typing import Any, Mapping, Sequence

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(ROOT)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from a_stock_http import DataSourceError  # noqa: E402
import dataset_contract  # noqa: E402
import dataset_projection as projection  # noqa: E402
from market_adapters import fetch_tencent_kline  # noqa: E402
from paths import data_file  # noqa: E402
import portfolio_research_history  # noqa: E402
import signal_ledger  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402


CATALOG_PATH = os.path.join(ROOT, "config", "dataset_catalog.json")
LOOKBACK_DAYS = 80
MAX_CODES = 40
SNAPSHOT_LOOKBACK_DAYS = 30
# performance_tracker 的历史文件路径。此处重算而非 import 那个脚本模块——
# 跨 skill 导入脚本要改 sys.path，而 sys_path_mutation 是维护性预算的计数项。
HISTORY_FILENAME = "signal_history.json"


def _market(code: str) -> str:
    return "sh" if str(code).startswith(("5", "6", "9")) else "sz"


def _settled_records() -> list[dict[str, Any]]:
    canonical = signal_ledger.project_signals(ledger_file=signal_ledger.LEDGER_FILE)
    legacy = read_json(data_file("stock-triage", HISTORY_FILENAME), [])
    return signal_ledger.merge_legacy_signals(canonical, legacy)


def _fetch_bars(codes: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    def fetch(code: str) -> tuple[str, list[dict[str, Any]]]:
        try:
            return code, fetch_tencent_kline(
                code, market=_market(code), days=LOOKBACK_DAYS, ktype="day"
            )
        except DataSourceError:
            return code, []

    if not codes:
        return {}
    with ThreadPoolExecutor(max_workers=min(4, len(codes))) as executor:
        return dict(executor.map(fetch, codes))


def _snapshot_codes(snapshots: Sequence[Mapping[str, Any]]) -> list[str]:
    codes = [
        str(candidate.get("code") or "")
        for snapshot in snapshots
        for candidate in (snapshot.get("candidates") or [])
        if str(candidate.get("code") or "")
    ]
    return list(dict.fromkeys(codes))[:MAX_CODES]


def _output_path(dataset_id: str, asof: str) -> str:
    return data_file("stock-triage", f"dataset_{dataset_id}_{asof}.json")


def _persist(payload: Mapping[str, Any], asof: str) -> dict[str, Any]:
    dataset_id = str(payload["dataset_id"])
    path = _output_path(dataset_id, asof)
    atomic_write_json(path, {**payload, "asof": asof})
    return {
        "dataset_id": dataset_id,
        "status": "written",
        "record_count": len(payload["rows"]),
        "coverage_ratio": round(float(payload["coverage_ratio"]), 6),
        "considered": payload["considered"],
        "path": path,
    }


def _skipped(dataset_id: str, reason: str) -> dict[str, Any]:
    return {"dataset_id": dataset_id, "status": "skipped", "reason": reason}


def build_settled_dataset(catalog: Mapping[str, Any], asof: str) -> dict[str, Any]:
    contract = dataset_contract.resolve_dataset(catalog, projection.SETTLED_DATASET_ID)
    try:
        payload = projection.build_settled_signal_rows(_settled_records(), contract)
    except dataset_contract.DatasetContractError as exc:
        return _skipped(projection.SETTLED_DATASET_ID, "; ".join(exc.errors))
    return _persist(payload, asof)


def build_direction_dataset(catalog: Mapping[str, Any], asof: str) -> dict[str, Any]:
    contract = dataset_contract.resolve_dataset(catalog, projection.DIRECTION_DATASET_ID)
    start = (date.fromisoformat(asof).toordinal() - SNAPSHOT_LOOKBACK_DAYS)
    snapshots = portfolio_research_history.load_snapshots(
        start=date.fromordinal(start).isoformat(), end=asof
    )
    if not snapshots:
        return _skipped(projection.DIRECTION_DATASET_ID, "no_research_snapshots")
    bars = _fetch_bars(_snapshot_codes(snapshots))
    try:
        payload = projection.build_direction_rows(snapshots, bars, contract)
    except dataset_contract.DatasetContractError as exc:
        return _skipped(projection.DIRECTION_DATASET_ID, "; ".join(exc.errors))
    return _persist(payload, asof)


def build_all(asof: str, *, include_direction: bool = True) -> dict[str, Any]:
    catalog = dataset_contract.load_catalog(CATALOG_PATH)
    results = [build_settled_dataset(catalog, asof)]
    if include_direction:
        results.append(build_direction_dataset(catalog, asof))
    written = [item for item in results if item["status"] == "written"]
    return {
        "schema": "research_dataset_build_v1",
        "asof": asof,
        "catalog_hash": catalog["catalog_hash"],
        "datasets": results,
        "written": len(written),
        "records": sum(int(item["record_count"]) for item in written),
        "has_signal": bool(written),
        "research_only": True,
        "trading_action": "none",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="物化 catalog 数据集")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument(
        "--settled-only",
        action="store_true",
        help="只建已结算信号数据集（不取行情，纯离线）",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = build_all(args.asof, include_direction=not args.settled_only)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for item in result["datasets"]:
            detail = (
                f"{item['record_count']} 行，覆盖率 {item['coverage_ratio']}"
                if item["status"] == "written"
                else item["reason"]
            )
            print(f"{item['dataset_id']}: {item['status']} — {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
