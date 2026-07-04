#!/usr/bin/env python3
"""第二策略（RS 领先回调）门控评估 — upgrade-plan v2 §7c。

复用 chanlun 门控评估的全部机件（mootdx 真实数据、无前视事件抽取、IS/OOS
切分、置换检验 + FDR、research_gate 判定），只替换信号 analyzer。与打板策略
的相关性天然低（打板吃 T+1 情绪溢价，本策略吃趋势延续），组合目的见方案。

- ``--mode real``      mootdx 真实日线，唯一可产生 A/B 结论的模式
- ``--mode synthetic`` 管线自检，结论恒为 pending_real_data_run

铁律：未通过门控不得注册 strategy_registry；本脚本不写注册状态。
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
CHAN = os.path.join(ROOT, "skills", "chanlun-backtest", "scripts")
for path in (COMMON, CHAN, os.path.dirname(os.path.abspath(__file__))):
    if path not in sys.path:
        sys.path.insert(0, path)

import chan_signal_backtest as backtest  # noqa: E402
import pullback_strategy  # noqa: E402
from chanlun_gate_evaluation import (  # noqa: E402
    _data_range,
    _synthetic_series,
    fetch_real_payload,
)
from paths import data_file  # noqa: E402
from state_store import atomic_write_json  # noqa: E402

SCHEMA = "pullback_gate_evaluation_v1"
ARTIFACT_DIR = data_file("stock-triage", "pullback_gate_evaluation")

# 运行期方向注册：不污染 chanlun 模块源码，仅本评估进程可见。
backtest.STRATEGY_DIRECTIONS.setdefault(
    pullback_strategy.STRATEGY_ID, pullback_strategy.DIRECTION,
)


def analyze_payload_with_pullback(
    payload: dict[str, Any],
    *,
    split_date: str,
    min_oos_samples: int = 30,
    n_perm: int = 5000,
) -> dict[str, Any]:
    """镜像 backtest.analyze_payload，把 analyzer 换成回调信号。"""
    import hashlib

    benchmark = backtest._benchmark_returns(payload.get("benchmark_bars"))
    series = list(payload.get("series") or [])
    events: list[dict[str, Any]] = []
    for item in series:
        events.extend(backtest.extract_signal_events(
            str(item.get("code") or ""),
            list(item.get("bars") or []),
            benchmark_by_date=benchmark,
            analyzer=pullback_strategy.analyze,
        ))
    control_pools = backtest.build_control_pools(
        series, benchmark_bars=payload.get("benchmark_bars"),
    )
    result = backtest.analyze_events(
        events,
        split_date=split_date,
        min_oos_samples=min_oos_samples,
        n_perm=n_perm,
        control_pools=control_pools,
    )
    result["sample"] = {
        "series": len(series),
        "events": len(events),
        "benchmark_available": bool(benchmark),
    }
    rules = json.dumps(
        {
            "version": "rs-leader-pullback-v1",
            "entry_rule": result.get("entry_rule"),
            "return_convention": result.get("return_convention"),
            "strategy": pullback_strategy.STRATEGY_ID,
            "params": pullback_strategy.PARAMS,
        },
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode()
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode()
    result["research_protocol"] = {
        "split_date": split_date,
        "rules_fingerprint": hashlib.sha256(rules).hexdigest(),
        "dataset_fingerprint": hashlib.sha256(encoded).hexdigest(),
    }
    return result


def run_evaluation(
    *,
    mode: str,
    split_date: str,
    start_date: str,
    codes: list[str] | None = None,
    min_oos_samples: int = 30,
    n_perm: int = 5000,
    persist: bool = True,
) -> dict[str, Any]:
    if mode == "real":
        payload = fetch_real_payload(codes=codes, start_date=start_date)
        meta = {"universe": payload.get("fetched_codes")}
    else:
        payload = _synthetic_series()
        meta = {"universe": "synthetic_fixture"}

    result = analyze_payload_with_pullback(
        payload,
        split_date=split_date,
        min_oos_samples=min_oos_samples,
        n_perm=n_perm,
    )
    if persist:
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        snapshot = os.path.join(ARTIFACT_DIR, f"{mode}_input_snapshot.json")
        atomic_write_json(snapshot, payload)
        result = backtest.persist_evidence(
            result, input_path=snapshot, artifact_dir=ARTIFACT_DIR,
        )
    result["evaluation"] = {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(),
        "data_mode": mode,
        "data_range": _data_range(payload.get("series") or []),
        "strategy_params": dict(pullback_strategy.PARAMS),
        **meta,
    }
    return result


def summarize_for_report(result: dict[str, Any]) -> dict[str, Any]:
    mode = result.get("evaluation", {}).get("data_mode")
    strategies = {}
    any_pass = False
    for strategy_id, item in result.get("strategies", {}).items():
        if strategy_id != pullback_strategy.STRATEGY_ID:
            continue  # 方向表含 chanlun 策略，本评估只产回调信号事件
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
        verdict = "B_not_admitted_stay_research_only"
    return {
        "verdict": verdict,
        "data_mode": mode,
        "data_range": result.get("evaluation", {}).get("data_range"),
        "split_date": result.get("split_date"),
        "sample": result.get("sample"),
        "strategies": strategies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["real", "synthetic"], default="synthetic")
    parser.add_argument("--split-date", default="2025-07-01")
    parser.add_argument("--start-date", default="2019-11-01")
    parser.add_argument("--codes", nargs="*")
    parser.add_argument("--min-oos-samples", type=int, default=30)
    parser.add_argument("--n-perm", type=int, default=5000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_evaluation(
        mode=args.mode,
        split_date=args.split_date,
        start_date=args.start_date,
        codes=args.codes,
        min_oos_samples=args.min_oos_samples,
        n_perm=args.n_perm,
    )
    summary = summarize_for_report(result)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"verdict: {summary['verdict']} (data_mode={summary['data_mode']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
