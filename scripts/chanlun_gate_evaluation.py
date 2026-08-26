#!/usr/bin/env python3
"""One-shot chanlun research-gate evaluation (upgrade-plan-v2 §5, task 5a).

Answers a single question with real numbers: does any of the four chan
structure signals already implemented in chan_structure.py (third_buy,
third_sell, top_divergence, bottom_divergence) clear the existing
chan_signal_backtest / research_gate IS/OOS + permutation + FDR gate?

This script does not invent a new rule. It is a thin data-acquisition +
reporting layer around the framework that already exists:
  - mootdx_source.py            real historical OHLCV (TCP, no third-party
                                 dependency beyond the already-vendored
                                 mootdx package)
  - chan_signal_backtest.py     analyze_payload() / persist_evidence() /
                                 register_oos_results() — the actual IS/OOS
                                 + permutation + FDR pipeline
  - research_gate.py            evaluate_gate() — pass/fail/blocked decision

Two run modes:
  --mode real       Pull real daily bars via mootdx for a fixed, documented
                     universe. This is the only mode whose output may be used
                     for an A/B (register vs. demote) decision.
  --mode synthetic   Deterministic synthetic fixture data. Only proves the
                     evaluation pipeline runs end-to-end; the JSON output is
                     tagged data_mode="synthetic" and the report generator
                     refuses to emit an A/B verdict for it (always
                     "pending_real_data_run").

Usage:
  python3 scripts/chanlun_gate_evaluation.py --mode real \
      --split 2025-07-01 --start 2023-01-01 --json

  python3 scripts/chanlun_gate_evaluation.py --mode synthetic --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
CHAN_SCRIPTS = os.path.join(ROOT, "skills", "chanlun-backtest", "scripts")
for path in (COMMON, CHAN_SCRIPTS, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import chan_signal_backtest as backtest  # noqa: E402
import mootdx_source as mootdx  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import atomic_write_json  # noqa: E402

SCHEMA = "chanlun_gate_evaluation_v1"

# Fixed, documented universe: liquid large/mid-cap A-shares spanning multiple
# sectors (banking, liquor, insurance, new energy, pharma, home appliance,
# infra, brokerage, coal, telecom). Chosen for (a) long uninterrupted daily
# history via mootdx and (b) enough structural variety that a chan signal
# firing only in one sector is visible in the per-strategy sample counts.
# Not index-derived on purpose: keeps this evaluation reproducible without a
# separate index-constituents fetch/cache dependency.
DEFAULT_UNIVERSE = [
    "600519", "000001", "600036", "000651", "601318",
    "300750", "002415", "600030", "000333", "601899",
    "600000", "000002", "601166", "600276", "000858",
    "601888", "600809", "000725", "601012", "300059",
]
DEFAULT_BENCHMARK_CODE = "000300"  # CSI300, via mootdx index quotes

OUTPUT_FILE = data_file("chanlun-backtest", "gate_evaluation_latest.json")
ARTIFACT_DIR = os.path.join(
    os.path.dirname(OUTPUT_FILE), "evidence", "gate_evaluation"
)
# 2026-08 T6：v2 谱系（12 个 chanlun_bsp{...}_v2 假设，新 split）写独立产物路径，
# 不与 legacy 四类型的已登记台账/证据文件混写或覆盖（docs_private/chanlun-upgrade-plan-2026-08.md §0）。
OUTPUT_FILE_V2 = data_file("chanlun-backtest", "gate_evaluation_v2_latest.json")
ARTIFACT_DIR_V2 = os.path.join(
    os.path.dirname(OUTPUT_FILE_V2), "evidence", "gate_evaluation_v2"
)


def _output_paths(lineage: str) -> tuple[str, str]:
    if lineage == "v2":
        return OUTPUT_FILE_V2, ARTIFACT_DIR_V2
    return OUTPUT_FILE, ARTIFACT_DIR


def _synthetic_series(n_codes: int = 6, n_bars: int = 260) -> dict[str, Any]:
    """Deterministic synthetic OHLCV. Pipeline-only — never used for A/B."""
    import random

    rng = random.Random(20260703)
    series = []
    for c in range(n_codes):
        code = f"90000{c}"
        price = 10.0 + c
        bars = []
        for i in range(n_bars):
            drift = rng.uniform(-0.02, 0.021)
            price = max(1.0, price * (1 + drift))
            high = price * (1 + abs(rng.uniform(0, 0.01)))
            low = price * (1 - abs(rng.uniform(0, 0.01)))
            bars.append({
                "date": f"2024-{1 + i // 22:02d}-{1 + i % 22:02d}",
                "open": round(price * (1 + rng.uniform(-0.003, 0.003)), 3),
                "high": round(high, 3),
                "low": round(low, 3),
                "close": round(price, 3),
                "volume": 1_000_000 + rng.randint(0, 200_000),
            })
        series.append({"code": code, "bars": bars})
    benchmark_bars = series[0]["bars"]
    return {"series": series, "benchmark_bars": benchmark_bars}


def _load_payload_for_mode(
    mode: str,
    *,
    codes: list[str] | None,
    start_date: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (payload, evaluation_meta) for the requested data mode."""
    if mode == "synthetic":
        return _synthetic_series(), {
            "data_source": "synthetic_fixture",
            "requested_codes": None,
        }
    if mode == "real":
        payload = fetch_real_payload(codes=codes, start_date=start_date)
        meta = {
            "data_source": "mootdx",
            "requested_codes": payload.pop("requested_codes"),
            "fetched_codes": payload.pop("fetched_codes"),
            "skipped_short_history": payload.pop("skipped_short_history"),
            "benchmark_code": payload.pop("benchmark_code"),
        }
        return payload, meta
    raise ValueError(f"unknown mode: {mode}")


def fetch_real_payload(
    *,
    codes: list[str] | None = None,
    start_date: str,
    benchmark_code: str = DEFAULT_BENCHMARK_CODE,
) -> dict[str, Any]:
    """Pull real daily bars for the fixed universe + benchmark via mootdx."""
    codes = list(codes or DEFAULT_UNIVERSE)
    client = mootdx.get_client(timeout=10)
    by_code = mootdx.fetch_klines(codes, start_date, client=client, max_pages=6)
    series = [
        {"code": code, "bars": bars}
        for code, bars in sorted(by_code.items())
        if len(bars) >= 60  # need enough history for structure detection + splits
    ]
    benchmark_bars = mootdx.fetch_index_daily(
        benchmark_code, start_date, client=client, max_pages=6
    )
    return {
        "series": series,
        "benchmark_bars": benchmark_bars,
        "requested_codes": codes,
        "fetched_codes": sorted(by_code),
        "skipped_short_history": sorted(
            code for code, bars in by_code.items() if len(bars) < 60
        ),
        "benchmark_code": benchmark_code,
    }


def _data_range(series: list[dict[str, Any]]) -> dict[str, Any]:
    all_dates = [
        str(bar.get("date"))
        for item in series
        for bar in item.get("bars", [])
        if bar.get("date")
    ]
    return {
        "symbol_count": len(series),
        "total_bars": len(all_dates),
        "earliest_date": min(all_dates) if all_dates else None,
        "latest_date": max(all_dates) if all_dates else None,
    }


def run_evaluation(
    *,
    mode: str,
    split_date: str,
    start_date: str,
    codes: list[str] | None = None,
    min_oos_samples: int = 30,
    n_perm: int = 5000,
    persist: bool = True,
    lineage: str = "legacy",
) -> dict[str, Any]:
    """`lineage="legacy"` (default) reproduces the 2026-07-03 four-signal
    evaluation unchanged. `lineage="v2"` runs the 2026-08 T6 versioned
    bsp_type lineage (12 `chanlun_bsp{...}_v2` IDs) — separate output/artifact
    paths, never touches the legacy OOS ledger or `strategy_registry`
    (no `--register` call exists in this script for either lineage).
    """
    payload, meta = _load_payload_for_mode(mode, codes=codes, start_date=start_date)

    result = backtest.analyze_payload(
        payload,
        split_date=split_date,
        min_oos_samples=min_oos_samples,
        n_perm=n_perm,
        lineage=lineage,
    )

    artifact_dir = _output_paths(lineage)[1]
    if persist:
        os.makedirs(artifact_dir, exist_ok=True)
        input_snapshot = os.path.join(artifact_dir, f"{mode}_input_snapshot.json")
        atomic_write_json(input_snapshot, payload)
        result = backtest.persist_evidence(
            result,
            input_path=input_snapshot,
            artifact_dir=artifact_dir,
        )

    result["evaluation"] = {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(),
        "data_mode": mode,
        "lineage": lineage,
        "data_range": _data_range(payload.get("series") or []),
        **meta,
    }
    return result


def summarize_for_report(result: dict[str, Any]) -> dict[str, Any]:
    """Per-strategy pass/fail summary + overall positioning verdict."""
    mode = result.get("evaluation", {}).get("data_mode")
    strategies = {}
    any_pass = False
    for strategy_id, item in result.get("strategies", {}).items():
        gate = item.get("gate_result", {})
        state = item.get("research_state", {})
        decision = gate.get("decision")
        if decision == "passed_for_reference" and gate.get("allowed_in_live_agent"):
            any_pass = True
        strategies[strategy_id] = {
            "direction": item.get("direction"),
            "oos_sample_count": state.get("oos_sample_count"),
            "permutation_p": state.get("permutation_p"),
            "fdr_p": state.get("fdr_p"),
            "oos_alpha": state.get("oos_alpha"),
            "benchmark_alpha": state.get("benchmark_alpha"),
            "decision": decision,
            "allowed_in_live_agent": gate.get("allowed_in_live_agent"),
            "blocking_reasons": gate.get("blocking_reasons"),
        }
    if mode != "real":
        verdict = "pending_real_data_run"
    elif any_pass:
        verdict = "A_register_candidate"
    else:
        verdict = "B_structure_filter_only"
    return {
        "verdict": verdict,
        "data_mode": mode,
        "data_range": result.get("evaluation", {}).get("data_range"),
        "split_date": result.get("split_date"),
        "strategies": strategies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="缠论四类结构信号一次性门控评估（真实数据或合成管线自检）"
    )
    parser.add_argument("--mode", choices=["real", "synthetic"], default="real")
    parser.add_argument("--split", default="2025-07-01", help="OOS 切分日 YYYY-MM-DD")
    parser.add_argument("--start", default="2023-01-01", help="真实数据起始日 YYYY-MM-DD")
    parser.add_argument("--codes", nargs="*", help="覆盖默认 universe（真实模式）")
    parser.add_argument("--min-oos-samples", type=int, default=30)
    parser.add_argument("--permutations", type=int, default=5000)
    parser.add_argument("--no-persist", action="store_true", help="不写证据产物文件")
    parser.add_argument(
        "--lineage", choices=["legacy", "v2"], default="legacy",
        help="legacy=2026-07 四类型协议（不动）；v2=2026-08 T6 版本化全谱系协议（12 个 bsp_type ID）",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_evaluation(
        mode=args.mode,
        split_date=args.split,
        start_date=args.start,
        codes=args.codes,
        min_oos_samples=args.min_oos_samples,
        n_perm=args.permutations,
        persist=not args.no_persist,
        lineage=args.lineage,
    )
    summary = summarize_for_report(result)
    result["summary"] = summary

    output_file = _output_paths(args.lineage)[0]
    if not args.no_persist:
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        atomic_write_json(output_file, result)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"verdict: {summary['verdict']} (data_mode={summary['data_mode']})")
        for strategy_id, row in summary["strategies"].items():
            print(
                f"  {strategy_id}: decision={row['decision']} "
                f"n_oos={row['oos_sample_count']} "
                f"perm_p={row['permutation_p']} fdr_p={row['fdr_p']} "
                f"oos_alpha={row['oos_alpha']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
