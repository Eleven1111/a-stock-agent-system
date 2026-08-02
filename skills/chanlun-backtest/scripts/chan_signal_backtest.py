#!/usr/bin/env python3
"""Walk-forward OOS research for the four executable Chan structure signals.

Signals are detected by repeatedly analyzing each historical prefix. A trade
can only enter at the next bar open after the signal first becomes observable,
which prevents using the signal's earlier structure index as a hindsight entry.
Returns are direction-normalized so bearish signals are positive when price
falls. This module is offline research only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from typing import Any, Callable, Iterable

HERE = os.path.dirname(os.path.abspath(__file__))
COMMON = os.path.abspath(os.path.join(HERE, "..", "..", "common"))
for path in (HERE, COMMON):
    if path not in sys.path:
        sys.path.insert(0, path)

import chan_structure  # noqa: E402
import daban_bt_engine as engine  # noqa: E402
import daban_bt_stats as stats  # noqa: E402
import research_gate  # noqa: E402
from paths import data_file  # noqa: E402
from research_artifact import verify_artifact, write_artifact  # noqa: E402
from state_store import mutate_json  # noqa: E402
from tradeability import assess_tradeability  # noqa: E402


STRATEGY_DIRECTIONS = {
    "chanlun_third_buy": "bullish",
    "chanlun_bottom_divergence": "bullish",
    "chanlun_third_sell": "bearish",
    "chanlun_top_divergence": "bearish",
}
# 2026-08 T6：结构升级后的全谱系买卖点（chan_bsp.py 输出的 strategy_id_v2），版本化 ID +
# 新留出集重评，legacy 四类型的既有代码路径/台账不动（见 docs/chanlun-upgrade-plan-2026-08.md §0）。
BSP_TYPES_V2 = ("1", "1p", "2", "2s", "3a", "3b")
STRATEGY_DIRECTIONS_V2 = {
    **{f"chanlun_bsp{t}_buy_v2": "bullish" for t in BSP_TYPES_V2},
    **{f"chanlun_bsp{t}_sell_v2": "bearish" for t in BSP_TYPES_V2},
}
REQUIRED_CONTROLS = ["random_entry", "simple_breakout", "buy_hold"]
REQUIRED_TESTS = ["t_test", "bootstrap", "permutation"]
RUN_REGISTRY_FILE = data_file("stock-triage", "chanlun_oos_runs.json")
RULES_VERSION = "chan-walk-forward-v2"
RULES_VERSION_V2 = "chan-structural-v2-t1t4-bsp-lineage"


ROUND_TRIP_COST = -engine.net_return(1.0, 1.0)


def _directional_net_from_gross(
    gross_return: float | None,
    direction: str,
) -> float | None:
    if gross_return is None:
        return None
    directional = -gross_return if direction == "bearish" else gross_return
    return directional - ROUND_TRIP_COST


def _directional_net_return(
    entry: float,
    exit_price: float,
    direction: str,
) -> float:
    return float(_directional_net_from_gross(exit_price / entry - 1.0, direction))


def _benchmark_returns(
    bars: Iterable[dict[str, Any]] | None,
) -> dict[str, dict[str, float]]:
    output = {}
    rows = list(bars or [])
    for index, bar in enumerate(rows):
        if index + 1 >= len(rows):
            continue
        try:
            entry = float(bar["open"])
            t1 = float(rows[index + 1]["close"]) / entry - 1.0
            t3 = (
                float(rows[index + 3]["close"]) / entry - 1.0
                if index + 3 < len(rows)
                else None
            )
        except (KeyError, TypeError, ValueError):
            continue
        output[str(bar.get("date") or "")] = {"t1": t1, "t3": t3}
    return output


def _control_event(
    code: str,
    bars: list[dict[str, Any]],
    detected_idx: int,
    direction: str,
) -> dict[str, Any] | None:
    if detected_idx + 2 >= len(bars):
        return None
    entry = bars[detected_idx + 1]
    if direction == "bullish":
        tradeability = assess_tradeability(
            {
                "price": entry.get("open"),
                "prev_close": bars[detected_idx].get("close"),
                "open": entry.get("open"),
                "high": entry.get("high"),
                "low": entry.get("low"),
                "volume": entry.get("volume"),
            },
            code,
        )
        if tradeability.get("tradeable") is False:
            return None
    try:
        raw_t1 = _directional_net_return(
            float(entry["open"]),
            float(bars[detected_idx + 2]["close"]),
            direction,
        )
        raw_t3 = (
            _directional_net_return(
                float(entry["open"]),
                float(bars[detected_idx + 4]["close"]),
                direction,
            )
            if detected_idx + 4 < len(bars)
            else None
        )
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "date": bars[detected_idx].get("date"),
        "t1": raw_t1,
        "t3": raw_t3,
    }


def build_control_pools(
    series: list[dict[str, Any]],
    *,
    benchmark_bars: list[dict[str, Any]] | None = None,
    strategy_directions: dict[str, str] | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Build three observable, reproducible control families.

    random_entry uses a stable 20% hash sample of eligible dates. The simple
    technical control is a 20-day close breakout for bullish strategies and a
    symmetric close breakdown for bearish strategies. buy_hold uses benchmark
    open-to-close returns for each available date.

    `strategy_directions` defaults to the legacy four-signal map; T6 passes
    `STRATEGY_DIRECTIONS_V2` (12 versioned bsp_type IDs) without touching this
    default so the legacy call sites and registered OOS ledger are untouched.
    """
    directions = strategy_directions or STRATEGY_DIRECTIONS
    pools = {
        strategy_id: {name: [] for name in REQUIRED_CONTROLS}
        for strategy_id in directions
    }
    benchmark = _benchmark_returns(benchmark_bars)
    for strategy_id, direction in directions.items():
        pools[strategy_id]["buy_hold"] = [
            {
                "date": trade_date,
                "t1": _directional_net_from_gross(value["t1"], direction),
                "t3": _directional_net_from_gross(value["t3"], direction),
            }
            for trade_date, value in sorted(benchmark.items())
        ]

    for item in series:
        code = str(item.get("code") or "").zfill(6)
        bars = list(item.get("bars") or [])
        for detected_idx in range(20, len(bars) - 1):
            prior = bars[detected_idx - 20:detected_idx]
            current = bars[detected_idx]
            trade_date = str(current.get("date") or "")
            digest = hashlib.sha256(f"{code}|{trade_date}".encode()).digest()
            is_random_entry = digest[0] % 5 == 0
            try:
                bullish_break = float(current["close"]) > max(
                    float(bar["high"]) for bar in prior
                )
                bearish_break = float(current["close"]) < min(
                    float(bar["low"]) for bar in prior
                )
            except (KeyError, TypeError, ValueError):
                continue
            for strategy_id, direction in directions.items():
                event = _control_event(code, bars, detected_idx, direction)
                if not event:
                    continue
                if is_random_entry:
                    pools[strategy_id]["random_entry"].append(event)
                if (
                    direction == "bullish" and bullish_break
                ) or (
                    direction == "bearish" and bearish_break
                ):
                    pools[strategy_id]["simple_breakout"].append(event)
    return pools


def extract_signal_events(
    code: str,
    bars: list[dict[str, Any]],
    *,
    benchmark_by_date: dict[str, dict[str, float]] | None = None,
    analyzer: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    strategy_directions: dict[str, str] | None = None,
    strategy_id_field: str = "strategy_id",
    require_is_sure: bool = False,
) -> list[dict[str, Any]]:
    """Detect first observability on historical prefixes and enter next open.

    `strategy_id_field`/`strategy_directions`/`require_is_sure` select which
    signal lineage to extract: legacy (`strategy_id`, four IDs, no is_sure
    filter — default, unchanged behavior) or T6's versioned bsp_type lineage
    (`strategy_id_field="strategy_id_v2"`, `STRATEGY_DIRECTIONS_V2`,
    `require_is_sure=True` — only anchored/confirmed strokes count, per the
    2026-08 evaluation protocol).
    """
    analyze = analyzer or chan_structure.analyze
    benchmark = benchmark_by_date or {}
    directions = strategy_directions or STRATEGY_DIRECTIONS
    events = []
    seen = set()
    for detected_idx in range(4, len(bars) - 2):
        result = analyze(bars[:detected_idx + 1])
        for raw in result.get("signals") or []:
            strategy_id = str(raw.get(strategy_id_field) or "")
            direction = directions.get(strategy_id)
            signal_idx = raw.get("idx")
            if direction is None or not isinstance(signal_idx, int):
                continue
            if require_is_sure and not raw.get("is_sure"):
                continue
            if signal_idx < 0 or signal_idx > detected_idx:
                continue
            key = (strategy_id, signal_idx)
            if key in seen:
                continue
            seen.add(key)

            entry_idx = detected_idx + 1
            entry = bars[entry_idx]
            if direction == "bullish":
                tradeability = assess_tradeability(
                    {
                        "price": entry.get("open"),
                        "prev_close": bars[detected_idx].get("close"),
                        "open": entry.get("open"),
                        "high": entry.get("high"),
                        "low": entry.get("low"),
                        "volume": entry.get("volume"),
                    },
                    str(code).zfill(6),
                )
                if tradeability.get("tradeable") is False:
                    continue
            entry_price = float(entry["open"])
            raw_t1 = _directional_net_return(
                entry_price,
                float(bars[entry_idx + 1]["close"]),
                direction,
            )
            raw_t3 = None
            if entry_idx + 3 < len(bars):
                raw_t3 = _directional_net_return(
                    entry_price,
                    float(bars[entry_idx + 3]["close"]),
                    direction,
                )
            control = benchmark.get(str(entry.get("date") or ""), {})
            events.append({
                "code": str(code).zfill(6),
                "signal_type": raw.get("type"),
                "strategy_id": strategy_id,
                "direction": direction,
                "execution_role": (
                    "long_entry" if direction == "bullish" else "avoidance_signal"
                ),
                "signal_idx": signal_idx,
                "signal_date": bars[signal_idx].get("date"),
                "detection_date": bars[detected_idx].get("date"),
                "entry_date": entry.get("date"),
                "entry_price": entry_price,
                "t1_exit_date": bars[entry_idx + 1].get("date"),
                "t3_exit_date": (
                    bars[entry_idx + 3].get("date")
                    if entry_idx + 3 < len(bars)
                    else None
                ),
                "t1_return": raw_t1,
                "t3_return": raw_t3,
                "control_t1_return": _directional_net_from_gross(
                    control.get("t1"),
                    direction,
                ),
                "control_t3_return": _directional_net_from_gross(
                    control.get("t3"),
                    direction,
                ),
            })
    return events


def _returns(
    events: list[dict[str, Any]],
    field: str,
) -> list[float]:
    return [
        float(event[field])
        for event in events
        if event.get(field) is not None
    ]


def _control_returns(
    controls: dict[str, list[dict[str, Any]]],
    *,
    split_date: str,
    period: str,
    field: str,
) -> dict[str, list[float]]:
    output = {}
    for name, events in controls.items():
        selected = [
            event for event in events
            if (
                str(event.get("date") or "") < split_date
                if period == "is"
                else str(event.get("date") or "") >= split_date
            )
        ]
        output[name] = [
            float(event[field])
            for event in selected
            if event.get(field) is not None
        ]
    return output


def _variant(
    events: list[dict[str, Any]],
    return_field: str,
    controls: dict[str, list[float]],
    *,
    n_perm: int,
) -> dict[str, Any]:
    signal = _returns(events, return_field)
    available = {
        name: values
        for name, values in controls.items()
        if values
    }
    strongest_name = max(
        available,
        key=lambda name: stats.summarize(available[name])["mean"],
        default=None,
    )
    strongest = available.get(strongest_name, [])
    t_stat, t_p = stats.t_test_vs_zero(signal)
    permutation = stats.permutation_test_diff(
        signal,
        strongest,
        n_perm=n_perm,
    )
    return {
        "signal": stats.summarize(signal),
        "controls": {
            name: stats.summarize(values)
            for name, values in controls.items()
        },
        "strongest_control": strongest_name,
        "control": stats.summarize(strongest),
        "t_test": {"t": t_stat, "p_approx": t_p},
        "bootstrap_ci": stats.bootstrap_ci_mean(signal, n_boot=2000),
        "permutation": permutation,
    }


def analyze_events(
    events: list[dict[str, Any]],
    *,
    split_date: str,
    min_oos_samples: int = 30,
    n_perm: int = 5000,
    control_pools: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
    strategy_directions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Produce independent IS/OOS evidence and gate decisions per strategy ID.

    Defaults to the legacy four-ID map; T6 passes `STRATEGY_DIRECTIONS_V2`
    (12 IDs) so the FDR correction below runs across all 12 hypotheses
    together, per the 2026-08 evaluation protocol.
    """
    directions = strategy_directions or STRATEGY_DIRECTIONS
    prepared = {}
    pvalues = []
    for strategy_id, direction in directions.items():
        selected = [
            event for event in events
            if event.get("strategy_id") == strategy_id
        ]
        is_events = [
            event for event in selected
            if str(event.get("detection_date") or "") < split_date
        ]
        oos_events = [
            event for event in selected
            if str(event.get("detection_date") or "") >= split_date
        ]
        controls = dict((control_pools or {}).get(strategy_id) or {})
        controls.setdefault("buy_hold", [
            {
                "date": event.get("detection_date"),
                "t1": event.get("control_t1_return"),
                "t3": event.get("control_t3_return"),
            }
            for event in selected
            if (
                event.get("control_t1_return") is not None
                or event.get("control_t3_return") is not None
            )
        ])
        for name in REQUIRED_CONTROLS:
            controls.setdefault(name, [])
        variants = {
            "t1": {
                "is": _variant(
                    is_events,
                    "t1_return",
                    _control_returns(
                        controls,
                        split_date=split_date,
                        period="is",
                        field="t1",
                    ),
                    n_perm=n_perm,
                ),
                "oos": _variant(
                    oos_events,
                    "t1_return",
                    _control_returns(
                        controls,
                        split_date=split_date,
                        period="oos",
                        field="t1",
                    ),
                    n_perm=n_perm,
                ),
            },
            "t3": {
                "is": _variant(
                    is_events,
                    "t3_return",
                    _control_returns(
                        controls,
                        split_date=split_date,
                        period="is",
                        field="t3",
                    ),
                    n_perm=n_perm,
                ),
                "oos": _variant(
                    oos_events,
                    "t3_return",
                    _control_returns(
                        controls,
                        split_date=split_date,
                        period="oos",
                        field="t3",
                    ),
                    n_perm=n_perm,
                ),
            },
        }
        primary = variants["t1"]["oos"]
        pvalue = float(primary["permutation"]["p_value"])
        pvalues.append(pvalue)
        prepared[strategy_id] = {
            "direction": direction,
            "sample": {
                "total": len(selected),
                "is": len(is_events),
                "oos": len(oos_events),
            },
            "variants": variants,
            "_permutation_p": pvalue,
        }

    fdr = stats.benjamini_hochberg(pvalues, q=0.10)
    output = {}
    for index, strategy_id in enumerate(directions):
        item = prepared[strategy_id]
        primary = item["variants"]["t1"]["oos"]
        signal_mean = float(primary["signal"]["mean"])
        control_mean = float(primary["control"]["mean"])
        present_controls = [
            name for name, summary in primary["controls"].items()
            if summary["n"] > 0
        ]
        research_state = {
            "asof": datetime.now().date().isoformat(),
            "strategy_id": strategy_id,
            "phase": "oos_complete",
            "rules_locked": True,
            "has_costs": True,
            "reports_all_variants": True,
            "controls": present_controls,
            "stat_tests": REQUIRED_TESTS,
            "oos_run_count": 1,
            "changed_after_oos": False,
            "min_oos_samples": min_oos_samples,
            "oos_sample_count": item["sample"]["oos"],
            "permutation_p": item["_permutation_p"],
            "fdr_p": fdr[index]["adjusted"],
            "oos_alpha": signal_mean - control_mean,
            "benchmark_alpha": 0.0,
        }
        output[strategy_id] = {
            "direction": item["direction"],
            "sample": item["sample"],
            "variants": item["variants"],
            "research_state": research_state,
            "gate_result": research_gate.evaluate_gate(research_state),
        }
    return {
        "schema": "chan_signal_backtest_v1",
        "generated_at": datetime.now().isoformat(),
        "split_date": split_date,
        "entry_rule": "first_detection_then_next_bar_open",
        "return_convention": "direction_normalized_net_return",
        "strategies": output,
    }


def _lineage_config(lineage: str) -> dict[str, Any]:
    """Resolve the strategy-ID lineage for `analyze_payload`.

    `"legacy"` is the untouched four-ID path used by the 2026-07 gate
    evaluation and its registered OOS ledger. `"v2"` is the 2026-08 T6
    versioned bsp_type lineage: `strategy_id_v2` field, 12 IDs, `is_sure`
    filtering (only anchored/confirmed strokes count) — a distinct
    rules_version so it can never be mistaken for a legacy protocol rerun.
    """
    if lineage == "v2":
        return {
            "directions": STRATEGY_DIRECTIONS_V2,
            "strategy_id_field": "strategy_id_v2",
            "require_is_sure": True,
            "rules_version": RULES_VERSION_V2,
        }
    if lineage == "legacy":
        return {
            "directions": STRATEGY_DIRECTIONS,
            "strategy_id_field": "strategy_id",
            "require_is_sure": False,
            "rules_version": RULES_VERSION,
        }
    raise ValueError(f"unknown lineage: {lineage!r}, expected 'legacy' or 'v2'")


def _research_protocol(
    payload: dict[str, Any],
    result: dict[str, Any],
    *,
    lineage: str,
    split_date: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()
    rules = json.dumps(
        {
            "version": config["rules_version"],
            "entry_rule": result["entry_rule"],
            "return_convention": result["return_convention"],
            "strategies": config["directions"],
            "controls": REQUIRED_CONTROLS,
            "require_is_sure": config["require_is_sure"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "lineage": lineage,
        "rules_version": config["rules_version"],
        "split_date": split_date,
        "rules_fingerprint": hashlib.sha256(rules).hexdigest(),
        "dataset_fingerprint": hashlib.sha256(encoded).hexdigest(),
    }


def analyze_payload(
    payload: dict[str, Any],
    *,
    split_date: str,
    min_oos_samples: int = 30,
    n_perm: int = 5000,
    lineage: str = "legacy",
) -> dict[str, Any]:
    """Run the IS/OOS + permutation + FDR pipeline for one strategy-ID lineage
    (see `_lineage_config`)."""
    config = _lineage_config(lineage)
    directions = config["directions"]

    benchmark = _benchmark_returns(payload.get("benchmark_bars"))
    series = list(payload.get("series") or [])
    events = []
    for item in series:
        events.extend(
            extract_signal_events(
                str(item.get("code") or ""),
                list(item.get("bars") or []),
                benchmark_by_date=benchmark,
                strategy_directions=directions,
                strategy_id_field=config["strategy_id_field"],
                require_is_sure=config["require_is_sure"],
            )
        )
    control_pools = build_control_pools(
        series,
        benchmark_bars=payload.get("benchmark_bars"),
        strategy_directions=directions,
    )
    result = analyze_events(
        events,
        split_date=split_date,
        min_oos_samples=min_oos_samples,
        n_perm=n_perm,
        control_pools=control_pools,
        strategy_directions=directions,
    )
    result["sample"] = {
        "series": len(series),
        "events": len(events),
        "benchmark_available": bool(benchmark),
    }
    result["research_protocol"] = _research_protocol(
        payload, result, lineage=lineage, split_date=split_date, config=config,
    )
    return result


def persist_evidence(
    result: dict[str, Any],
    *,
    input_path: str,
    artifact_dir: str,
) -> dict[str, Any]:
    """Write and re-verify one evidence artifact per independently gated signal."""
    output = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    protocol = dict(output.get("research_protocol") or {})
    for strategy_id, item in output.get("strategies", {}).items():
        state = dict(item.get("research_state") or {})
        primary = ((item.get("variants") or {}).get("t1") or {}).get("oos") or {}
        controls = primary.get("controls") or {}
        control_counts = {
            name: int((summary or {}).get("n", 0))
            for name, summary in controls.items()
        }
        metrics = {
            field: state.get(field)
            for field in (
                "permutation_p",
                "fdr_p",
                "oos_alpha",
                "benchmark_alpha",
                "oos_sample_count",
            )
        }
        artifact_path = os.path.abspath(os.path.join(artifact_dir, f"{strategy_id}.json"))
        artifact = write_artifact(
            artifact_path,
            input_path=input_path,
            strategy_id=strategy_id,
            rules={
                "rules_version": protocol.get("rules_version", RULES_VERSION),
                "strategy_id": strategy_id,
                "direction": item.get("direction"),
                **protocol,
            },
            result={
                "research_protocol": protocol,
                "strategy": item,
            },
            gate_metrics=metrics,
            control_counts=control_counts,
        )
        state.update({
            "evidence_artifact": artifact_path,
            "evidence_sha256": artifact["artifact_sha256"],
        })
        item["research_state"] = state
        item["gate_result"] = research_gate.evaluate_gate(state)
        item["evidence"] = {
            "artifact": artifact_path,
            "sha256": artifact["artifact_sha256"],
        }
    return output


def register_oos_results(
    result: dict[str, Any],
    *,
    registry_file: str | None = None,
    gate_registrar: Callable[[str, dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Register one immutable OOS protocol, rejecting changed repeat runs."""
    protocol = dict(result.get("research_protocol") or {})
    required = {"split_date", "rules_fingerprint", "dataset_fingerprint"}
    if not required.issubset(protocol):
        return {"status": "blocked", "reason": "missing research protocol fingerprints"}
    for strategy_id, item in result.get("strategies", {}).items():
        state = item.get("research_state") or {}
        evidence_path = state.get("evidence_artifact")
        if not evidence_path:
            return {"status": "blocked", "reason": f"missing evidence artifact for {strategy_id}"}
        verification = verify_artifact(
            str(evidence_path),
            expected_sha256=state.get("evidence_sha256"),
        )
        if not verification["valid"]:
            return {
                "status": "blocked",
                "reason": f"invalid evidence artifact for {strategy_id}: {verification['errors']}",
            }
        fresh_gate = research_gate.evaluate_gate(state)
        item["gate_result"] = fresh_gate
        if fresh_gate.get("decision") == "blocked":
            return {"status": "blocked", "reason": f"evidence gate blocked for {strategy_id}"}
    run_file = registry_file or RUN_REGISTRY_FILE
    outcome: dict[str, Any] = {}

    def _register(value: Any) -> dict[str, Any]:
        records = dict(value) if isinstance(value, dict) else {}
        existing = records.get("chanlun_four_signal_oos")
        if existing:
            comparable = {key: existing.get(key) for key in required}
            current = {key: protocol.get(key) for key in required}
            if comparable != current:
                outcome.update({
                    "status": "blocked",
                    "reason": (
                        "OOS protocol already registered; changed rules, split, "
                        "or dataset require versioned strategy IDs and a new holdout"
                    ),
                    "existing": comparable,
                    "requested": current,
                })
                return records
            outcome.update({"status": "idempotent", "protocol": current})
            return records
        records["chanlun_four_signal_oos"] = {
            **protocol,
            "registered_at": datetime.now().isoformat(),
            "strategy_ids": list(STRATEGY_DIRECTIONS),
        }
        outcome.update({"status": "registered", "protocol": protocol})
        return records

    mutate_json(run_file, _register, {})
    if outcome.get("status") == "blocked":
        return outcome
    if gate_registrar is None:
        import strategy_registry

        gate_registrar = strategy_registry.register_gate_result
    for strategy_id, item in result.get("strategies", {}).items():
        gate_registrar(strategy_id, item["gate_result"])
    outcome["strategy_ids"] = list(result.get("strategies", {}))
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(
        description="缠论四类结构信号的无前视 IS/OOS 回测"
    )
    parser.add_argument("--input", required=True, help="series + benchmark_bars JSON")
    parser.add_argument("--split", required=True, help="OOS 切分日 YYYY-MM-DD")
    parser.add_argument("--min-oos-samples", type=int, default=30)
    parser.add_argument("--permutations", type=int, default=5000)
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--artifact-dir", help="每个策略的 OOS 证据产物目录；--register 时必需")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.register and not args.artifact_dir:
        parser.error("--register requires --artifact-dir")

    with open(args.input, encoding="utf-8") as handle:
        payload = json.load(handle)
    result = analyze_payload(
        payload,
        split_date=args.split,
        min_oos_samples=args.min_oos_samples,
        n_perm=args.permutations,
    )
    if args.artifact_dir:
        result = persist_evidence(
            result,
            input_path=args.input,
            artifact_dir=args.artifact_dir,
        )
    if args.register:
        result["formal_registration"] = register_oos_results(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("formal_registration", {}).get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
