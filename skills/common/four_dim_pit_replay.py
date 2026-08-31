"""Point-in-time exploratory replay for the technical Four-Dimension score.

The public Interface is ``replay`` for an in-memory bar panel and ``run`` for
the local-history Adapter.  Both fail closed on the other three dimensions and
produce an immutable, content-addressed research artifact.  No result from
this Module is eligible to change live weights or enter the research gate.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence

import dataset_contract
from execution_model import net_return_pct
import local_market_history
from research_artifact import file_sha256, json_sha256
from validation_program import build_walk_forward_folds


POLICY_SCHEMA = "four_dim_pit_replay_policy_v1"
ARTIFACT_SCHEMA = "four_dim_pit_replay_artifact_v1"
ENGINE_VERSION = "four-dim-technical-pit-v2"
CATALOG_PATH = Path(__file__).resolve().parents[2] / "config" / "dataset_catalog.json"
DATASET_ID = "four_dim_technical_pit_replay_v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
TECHNICAL_SCORER_PATH = REPO_ROOT / "skills" / "stock-triage" / "scripts" / "four_dim_scorer.py"
TECHNICAL_ADAPTER_SEMANTICS = {
    "version": "four-dim-technical-pit-adapter-v2",
    "lookback_sessions": 60,
    "chan_structure": "disabled_not_point_in_time_display_only",
    "current_research_registry": "not_read_for_historical_score",
    "observable_proxy_payload": "omitted_not_used_by_numeric_score",
    "emotion_cycle_payload": "omitted_display_only_zero_weight",
}
ScoreAdapter = Callable[[str, Sequence[Mapping[str, Any]]], Mapping[str, Any]]


def _load_object(path: str | os.PathLike[str]) -> dict[str, Any]:
    with open(os.path.abspath(os.path.expanduser(str(path))), encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _policy(path: str | os.PathLike[str]) -> tuple[dict[str, Any], str]:
    value = _load_object(path)
    if value.get("schema") != POLICY_SCHEMA:
        raise ValueError("unsupported four-dimension PIT replay policy")
    horizons = value.get("horizons")
    if horizons != [1, 3]:
        raise ValueError("four-dimension PIT horizons must be [1, 3]")
    minimum = int(value.get("minimum_history_sessions") or 0)
    if minimum < 60:
        raise ValueError("minimum_history_sessions must be at least 60")
    if int(value.get("technical_lookback_sessions") or 0) != 60:
        raise ValueError("technical_lookback_sessions must match the canonical 60-session scorer window")
    variants = value.get("variants")
    if not isinstance(variants, list) or not variants:
        raise ValueError("at least one predeclared technical variant is required")
    ids = [str(item.get("variant_id") or "") for item in variants if isinstance(item, Mapping)]
    if len(ids) != len(variants) or not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("variant_id values must be unique and non-empty")
    signatures = []
    for item in variants:
        minimum_score = item.get("minimum_score")
        daily_top_n = item.get("daily_top_n")
        if (isinstance(minimum_score, bool)
                or not isinstance(minimum_score, (int, float))
                or not math.isfinite(float(minimum_score))
                or not -3 <= float(minimum_score) <= 10):
            raise ValueError("variant minimum_score must be within the technical score range")
        if isinstance(daily_top_n, bool) or not isinstance(daily_top_n, int) or daily_top_n <= 0:
            raise ValueError("variant daily_top_n must be a positive integer")
        signatures.append((float(minimum_score), daily_top_n))
    if len(signatures) != len(set(signatures)):
        raise ValueError("redundant variant policy: score threshold and daily_top_n must differ")
    walk = value.get("walk_forward") or {}
    if int(walk.get("purge") or -1) < max(horizons):
        raise ValueError("walk-forward purge must cover the maximum outcome horizon")
    return value, json_sha256(value)


def _normalise_bar(row: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value["trading_date"] = str(value.get("trading_date") or value.get("date") or "")[:10]
    value["date"] = value["trading_date"]
    return value


def _group(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for raw in rows:
        row = _normalise_bar(raw)
        code = str(row.get("code") or "")
        if code and row["trading_date"]:
            grouped[code][row["trading_date"]] = row
    return {code: sorted(values.values(), key=lambda item: item["trading_date"])
            for code, values in grouped.items()}


_SCORER_MODULE: Any | None = None


def _canonical_score_adapter(code: str, bars: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Adapter around the live scorer's technical Implementation, with no I/O."""
    global _SCORER_MODULE
    if _SCORER_MODULE is None:
        path = TECHNICAL_SCORER_PATH
        if str(path.parent) not in sys.path:
            sys.path.insert(0, str(path.parent))
        spec = importlib.util.spec_from_file_location("_four_dim_pit_canonical_scorer", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("canonical four-dimension scorer unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Today's research registry is not knowable on historical D.  With
        # _chan_allowed=False every Chan delta and score lock is already zero;
        # setting _chan=None skips an expensive display-only analysis without
        # changing any numeric technical field (guarded by equivalence tests).
        module._chan_allowed = lambda _strategy_id: False
        module._chan = None
        # Neither payload participates in the numeric technical score.  The
        # replay artifact does not publish them, so avoid rebuilding two
        # display-only structures hundreds of thousands of times.
        module.compute_observable_proxies = lambda *_args, **_kwargs: {}
        module._compute_emotion_features = lambda _bars: {
            "status": "unavailable", "reason": "disabled_exploratory_pit_adapter"
        }
        _SCORER_MODULE = module
    latest = dict(bars[-1])
    quote = {
        "price": latest.get("close"),
        "change_pct": latest.get("pct_chg"),
        "turnover": latest.get("turn"),
        "amount": latest.get("amount"),
        "provider": latest.get("source"),
        "asof": latest.get("trading_date"),
    }
    return _SCORER_MODULE.score_technical(code, code, quote=quote, klines=list(bars))


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _settle(
    decision_date: str,
    horizon: int,
    stock: Sequence[Mapping[str, Any]],
    benchmark: Sequence[Mapping[str, Any]],
    costs: Mapping[str, Any],
) -> dict[str, Any] | None:
    stock_future = [row for row in stock if row["trading_date"] > decision_date]
    benchmark_future = [row for row in benchmark if row["trading_date"] > decision_date]
    if len(stock_future) < horizon or len(benchmark_future) < horizon:
        return None
    entry = stock_future[0]
    exit_bar = stock_future[horizon - 1]
    bench_entry = benchmark_future[0]
    bench_exit = benchmark_future[horizon - 1]
    if (entry["trading_date"] != bench_entry["trading_date"]
            or exit_bar["trading_date"] != bench_exit["trading_date"]):
        return None
    entry_raw = _positive(entry.get("open"))
    exit_raw = _positive(exit_bar.get("close"))
    benchmark_entry = _positive(bench_entry.get("open"))
    benchmark_exit = _positive(bench_exit.get("close"))
    if None in {entry_raw, exit_raw, benchmark_entry, benchmark_exit}:
        return None
    entry_price = float(entry_raw) * (1 + float(costs["entry_slippage_bps"]) / 10_000)
    exit_price = float(exit_raw) * (1 - float(costs["exit_slippage_bps"]) / 10_000)
    gross = exit_price / entry_price - 1
    priced = net_return_pct(
        gross_return_pct=gross * 100,
        notional=float(costs["assumed_notional"]),
        asof=str(entry["trading_date"]),
    )
    net = float(priced["net_return_pct"]) / 100
    benchmark_return = float(benchmark_exit) / float(benchmark_entry) - 1
    snapshot = {"stock": [dict(item) for item in stock_future[:horizon]],
                "benchmark": [dict(item) for item in benchmark_future[:horizon]]}
    return {
        "horizon_sessions": horizon,
        "entry_date": entry["trading_date"],
        "entry_price_raw": float(entry_raw),
        "entry_price_after_slippage": entry_price,
        "exit_date": exit_bar["trading_date"],
        "exit_price_raw": float(exit_raw),
        "exit_price_after_slippage": exit_price,
        "gross_forward_return": gross,
        "net_forward_return": net,
        "benchmark_forward_return": benchmark_return,
        "net_alpha": net - benchmark_return,
        "cost_model": {**dict(costs), **priced},
        "outcome_available_at": f"{exit_bar['trading_date']}T15:00:00+08:00",
        "bar_snapshot_sha256": json_sha256(snapshot),
    }


def _metrics(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [float(row["net_forward_return"]) for row in samples]
    alphas = [float(row["net_alpha"]) for row in samples]
    if not values:
        return {"status": "not_evaluated", "sample_count": 0, "reason": "no_settled_test_samples"}
    return {
        "status": "evaluated",
        "sample_count": len(values),
        "mean_net_forward_return": sum(values) / len(values),
        "mean_net_alpha": sum(alphas) / len(alphas),
        "win_rate": sum(value > 0 for value in values) / len(values),
        "positive_alpha_rate": sum(value > 0 for value in alphas) / len(alphas),
    }


def _variant_comparison(
    variants: Sequence[Mapping[str, Any]], samples: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Detect variants that only rename an identical realised sample set."""
    hashes: dict[str, str] = {}
    counts: dict[str, int] = {}
    for variant in variants:
        variant_id = str(variant["variant_id"])
        identities = sorted(
            (
                str(row["entity_id"]), str(row["decision_date"]),
                int(row["horizon_sessions"]), str(row["entry_date"]), str(row["exit_date"]),
            )
            for row in samples if row["variant_id"] == variant_id
        )
        hashes[variant_id] = json_sha256(identities)
        counts[variant_id] = len(identities)
    by_hash: dict[str, list[str]] = defaultdict(list)
    for variant_id, digest in hashes.items():
        by_hash[digest].append(variant_id)
    redundant_groups = [
        {"sample_set_sha256": digest, "variant_ids": sorted(variant_ids),
         "sample_count": counts[variant_ids[0]]}
        for digest, variant_ids in sorted(by_hash.items()) if len(variant_ids) > 1
    ]
    if len(variants) < 2:
        status = "not_evaluated"
        reason = "fewer_than_two_variants"
    elif redundant_groups:
        status = "redundant"
        reason = "realised_candidate_and_outcome_sets_are_identical"
    else:
        status = "distinct"
        reason = None
    return {
        "status": status,
        "reason": reason,
        "comparison_eligible": status == "distinct",
        "sample_set_sha256_by_variant": hashes,
        "sample_count_by_variant": counts,
        "redundant_groups": redundant_groups,
    }


def _panel_sha256(grouped: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    """Streaming deterministic hash; do not materialise a second 1.3M-row panel."""
    digest = hashlib.sha256()
    for code in sorted(grouped):
        for row in grouped[code]:
            digest.update(json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def replay(
    rows: Sequence[Mapping[str, Any]],
    *,
    policy: Mapping[str, Any],
    policy_sha256: str,
    score_adapter: ScoreAdapter | None = None,
) -> dict[str, Any]:
    """Replay predeclared technical variants using D-and-earlier bars only."""
    grouped = _group(rows)
    benchmark_code = str((policy.get("benchmark") or {}).get("code") or "")
    benchmark = grouped.get(benchmark_code) or []
    sessions = [row["trading_date"] for row in benchmark]
    maximum_horizon = max(int(item) for item in policy["horizons"])
    decision_sessions = sessions[:-maximum_horizon] if len(sessions) > maximum_horizon else []
    walk = dict(policy["walk_forward"])
    folds = build_walk_forward_folds(len(decision_sessions), **walk)
    scorer = score_adapter or _canonical_score_adapter
    dimensions = {
        "technical": {"qualification": "exploratory_reconstruction", "available": True},
        "sentiment": {"qualification": "unavailable", "available": False,
                      "reason": "historical_point_in_time_context_missing"},
        "catalyst": {"qualification": "unavailable", "available": False,
                     "reason": "historical_point_in_time_news_missing"},
        "deep": {"qualification": "unavailable", "available": False,
                 "reason": "historical_point_in_time_research_snapshot_missing"},
    }
    test_dates: dict[str, str] = {}
    for fold in folds:
        for position in range(int(fold["test_start"]), int(fold["test_end"])):
            test_dates[decision_sessions[position]] = str(fold["fold_id"])

    minimum_history = int(policy["minimum_history_sessions"])
    technical_lookback = int(policy["technical_lookback_sessions"])
    variants = [dict(item) for item in policy["variants"]]
    candidates: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    rejected = defaultdict(int)
    for code, stock_rows in grouped.items():
        if code == benchmark_code:
            continue
        stock_dates = [row["trading_date"] for row in stock_rows]
        for decision_date, fold_id in sorted(test_dates.items()):
            history_end = bisect_right(stock_dates, decision_date)
            if history_end == 0 or stock_dates[history_end - 1] != decision_date:
                continue
            if history_end < minimum_history:
                rejected["insufficient_history"] += 1
                continue
            history_start = max(0, history_end - technical_lookback)
            history = stock_rows[history_start:history_end]
            # The assertion is part of the Interface: an Adapter may never see
            # D+1 bars. bisect_right plus exact-date presence makes this O(1).
            if not history or history[-1]["trading_date"] != decision_date:
                raise ValueError("point_in_time_violation")
            technical = dict(scorer(code, history))
            score = technical.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                rejected["technical_score_unavailable"] += 1
                continue
            core = {
                "entity_id": code,
                "decision_date": decision_date,
                "decision_available_at": f"{decision_date}T15:00:00+08:00",
                "fold_id": fold_id,
                "feature_bar_max_date": history[-1]["trading_date"],
                "feature_bar_count": len(history),
                "technical_score": float(score),
                "technical_detail": technical.get("detail"),
                "_feature_history_start": history_start,
                "_feature_history_end": history_end,
            }
            for variant in variants:
                if float(score) >= float(variant["minimum_score"]):
                    candidates[(decision_date, variant["variant_id"])].append(core)

    samples: list[dict[str, Any]] = []
    unresolved = 0
    for (decision_date, variant_id), rows_for_day in sorted(candidates.items()):
        variant = next(item for item in variants if item["variant_id"] == variant_id)
        selected = sorted(rows_for_day, key=lambda item: (-item["technical_score"], item["entity_id"]))[
            :int(variant["daily_top_n"])
        ]
        for candidate in selected:
            candidate = dict(candidate)
            history_start = int(candidate.pop("_feature_history_start"))
            history_end = int(candidate.pop("_feature_history_end"))
            candidate["feature_snapshot_sha256"] = json_sha256(
                grouped[candidate["entity_id"]][history_start:history_end]
            )
            for horizon in policy["horizons"]:
                outcome = _settle(
                    decision_date, int(horizon),
                    grouped[candidate["entity_id"]], benchmark, policy["cost_model"],
                )
                if outcome is None:
                    unresolved += 1
                    continue
                sample = {
                    **candidate,
                    **outcome,
                    "variant_id": variant_id,
                    "evidence_class": "exploratory_reconstruction",
                    "research_only": True,
                    "canonical_forward_eligible": False,
                    "execution_eligible": False,
                }
                sample["sample_sha256"] = json_sha256(sample)
                samples.append(sample)

    summaries: dict[str, Any] = {}
    for variant in variants:
        vid = variant["variant_id"]
        summaries[vid] = {
            f"t{horizon}": _metrics([
                row for row in samples
                if row["variant_id"] == vid and row["horizon_sessions"] == horizon
            ]) for horizon in policy["horizons"]
        }
    variant_comparison = _variant_comparison(variants, samples)
    redundant_with: dict[str, str] = {}
    for group in variant_comparison["redundant_groups"]:
        representative, *duplicates = group["variant_ids"]
        for duplicate in duplicates:
            redundant_with[duplicate] = representative
    for variant in variants:
        vid = str(variant["variant_id"])
        summaries[vid]["comparison"] = {
            "status": "redundant" if vid in redundant_with else "representative_or_distinct",
            "redundant_with": redundant_with.get(vid),
            "sample_set_sha256": variant_comparison["sample_set_sha256_by_variant"][vid],
        }
    source_snapshot = {
        "row_count": len(rows),
        "min_date": min((str(row.get("trading_date") or row.get("date")) for row in rows), default=None),
        "max_date": max((str(row.get("trading_date") or row.get("date")) for row in rows), default=None),
        "rows_sha256": _panel_sha256(grouped),
    }
    catalog = dataset_contract.load_catalog(CATALOG_PATH)
    contract = dataset_contract.resolve_dataset(catalog, DATASET_ID)
    dataset_rows = [
        {
            "entity_id": row["entity_id"],
            "variant_id": row["variant_id"],
            "src": row["decision_date"],
            "score": row["technical_score"],
            "decision_available_at": row["decision_available_at"],
            "entry_date": row["entry_date"],
            "dst": row["exit_date"],
            "outcome_available_at": row["outcome_available_at"],
            "horizon_sessions": row["horizon_sessions"],
            "gross_forward_return": row["gross_forward_return"],
            "net_forward_return": row["net_forward_return"],
            "benchmark_forward_return": row["benchmark_forward_return"],
            "net_alpha": row["net_alpha"],
            "snapshot_ref": f"four_dim_pit:{row['sample_sha256']}",
            "evidence_class": row["evidence_class"],
        }
        for row in samples
    ]
    dataset_validation = dataset_contract.validate_records(dataset_rows, contract)
    artifact: dict[str, Any] = {
        "schema": ARTIFACT_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "dataset_id": DATASET_ID,
        "dataset_contract_sha256": contract["contract_hash"],
        "policy_sha256": policy_sha256,
        "implementation_bindings": {
            "technical_scorer_sha256": file_sha256(str(TECHNICAL_SCORER_PATH)),
            "technical_adapter_semantics": TECHNICAL_ADAPTER_SEMANTICS,
            "technical_adapter_semantics_sha256": json_sha256(TECHNICAL_ADAPTER_SEMANTICS),
            "execution_model_sha256": file_sha256(str(Path(__file__).with_name("execution_model.py"))),
            "validation_program_sha256": file_sha256(str(Path(__file__).with_name("validation_program.py"))),
        },
        "source_snapshot": source_snapshot,
        "evidence_class": "exploratory_reconstruction",
        "dimensions": dimensions,
        "weight_calibration": {
            "status": "unavailable",
            "reason": "three_dimensions_lack_historical_point_in_time_evidence",
            "automatic_live_weight_change": False,
        },
        "split": {"method": "walk_forward", "folds": folds, **walk},
        "variants": variants,
        "samples": samples,
        "dataset_rows": dataset_rows,
        "dataset_validation": dataset_validation,
        "variant_metrics": summaries,
        "variant_comparison": variant_comparison,
        "control_counts": {
            "settled_samples": len(samples),
            "unresolved_outcomes": unresolved,
            **dict(sorted(rejected.items())),
        },
        "research_only": True,
        "canonical_forward_eligible": False,
        "research_gate_eligible": False,
        "execution_eligible": False,
        "automatic_live_weight_change": False,
    }
    artifact["artifact_sha256"] = json_sha256(artifact)
    return artifact


def immutable_write(output_root: str | os.PathLike[str], artifact: Mapping[str, Any]) -> Path:
    digest = str(artifact.get("artifact_sha256") or "")
    if digest != json_sha256({key: value for key, value in artifact.items() if key != "artifact_sha256"}):
        raise ValueError("artifact_sha256_mismatch")
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{digest}.json"
    if target.exists():
        if _load_object(target) != dict(artifact):
            raise ValueError(f"immutable artifact conflict: {target}")
        return target
    encoded = json.dumps(artifact, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    fd, temporary = tempfile.mkstemp(dir=root, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if _load_object(target) != dict(artifact):
                raise ValueError(f"immutable artifact conflict: {target}")
    finally:
        os.unlink(temporary)
    return target


def run(
    *,
    start_date: str,
    end_date: str,
    policy_path: str,
    output_root: str,
    codes: Sequence[str] | None = None,
) -> dict[str, Any]:
    policy, policy_hash = _policy(policy_path)
    benchmark_code = str(policy["benchmark"]["code"])
    selected = list(dict.fromkeys(str(code) for code in (codes or []) if str(code)))
    if selected:
        query_codes = list(dict.fromkeys([*selected, benchmark_code]))
        # The requested window is the complete replay input; folds consume its
        # leading sessions for train/calibration before any test decision.
        rows = local_market_history.get_daily_bars(
            query_codes, end_date, 100_000, adjust_flag=str(policy["adjust_flag"])
        )
        rows = [row for row in rows if str(row["trading_date"]) >= start_date]
    else:
        dates = local_market_history.trading_dates_between(start_date, end_date, str(policy["adjust_flag"]))
        if not dates:
            raise ValueError("no cached trading sessions in requested range")
        rows = []
        # Local Adapter intentionally exposes an explicit date loop rather than
        # consulting today's candidate cache, which would contaminate history.
        for trading_date in dates:
            rows.extend(local_market_history.get_bars_on(trading_date, str(policy["adjust_flag"])))
        # Benchmark may not be part of the equity universe query on every day.
        benchmark_rows = local_market_history.get_daily_bars(
            [benchmark_code], end_date, 100_000, adjust_flag=str(policy["adjust_flag"])
        )
        rows.extend(row for row in benchmark_rows if str(row["trading_date"]) >= start_date)
    artifact = replay(rows, policy=policy, policy_sha256=policy_hash)
    path = immutable_write(output_root, artifact)
    return {"artifact_path": str(path), **artifact}
