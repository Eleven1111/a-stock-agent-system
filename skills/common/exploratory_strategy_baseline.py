#!/usr/bin/env python3
"""Exploratory daily-bar baselines for the observable parts of S1/S3/S5.

This is a research-only Module.  Its Interface accepts frozen qfq daily bars
and a policy, then returns point-in-time walk-forward diagnostics.  The
Implementation never calls the research gate, strategy registry, ranking or
live-weight code.

The strategy names intentionally end in ``_daily_proxy``.  These are not the
complete S1/S3/S5 strategies: auction/09:45 evidence, sector/leader identity,
market-sentiment confirmation and directional minute-volume confirmation are
not present in daily bars.  S2 and S4 are structurally excluded.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Iterable, Mapping, Sequence

from validation_program import ValidationError, build_walk_forward_folds


SCHEMA = "exploratory_strategy_baseline_v1"
ENGINE_VERSION = "daily-proxy-walk-forward-v1"
ALLOWED_STRATEGIES = (
    "reverse_volume_daily_proxy",
    "rank_surprise_daily_proxy",
    "assist_strength_daily_proxy",
)
FORBIDDEN_SOURCE_STRATEGIES = {"divergence_reseal", "preleader_arbitrage"}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_policy(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("schema") != "exploratory_strategy_baseline_policy_v1":
        raise ValueError("exploratory baseline policy schema mismatch")
    strategies = value.get("strategies") or {}
    if set(strategies) != set(ALLOWED_STRATEGIES):
        raise ValueError("policy must contain exactly the S1/S3/S5 daily proxies")
    if any(row.get("source_strategy") in FORBIDDEN_SOURCE_STRATEGIES for row in strategies.values()):
        raise ValueError("S2/S4 must not enter daily-bar exploratory baselines")
    walk_forward = value.get("walk_forward") or {}
    if int(walk_forward.get("purge", -1)) < 3 or int(walk_forward.get("embargo", -1)) < 0:
        raise ValueError("walk-forward purge must cover the T+3 label horizon")
    qualification = value.get("qualification") or {}
    if (
        qualification.get("evidence_class") != "exploratory_reconstruction"
        or qualification.get("research_gate_eligible") is not False
        or qualification.get("registry_eligible") is not False
        or qualification.get("live_weight_eligible") is not False
    ):
        raise ValueError("exploratory qualification must fail closed")
    return value


def _normalise_bars(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        code = str(raw.get("code") or "").zfill(6)
        date = str(raw.get("trading_date", raw.get("date")) or "")
        if len(code) != 6 or not code.isdigit() or not date:
            continue
        key = (code, date)
        if key in seen:
            raise ValueError(f"duplicate daily bar: {code}/{date}")
        seen.add(key)
        row = dict(raw)
        row["code"] = code
        row["date"] = date
        grouped[code].append(row)
    for code in grouped:
        grouped[code].sort(key=lambda row: row["date"])
    return dict(grouped)


def _percentile_rank(values: Sequence[float], value: float) -> float:
    if not values:
        return 1.0
    return sum(item <= value for item in values) / len(values)


def _daily_range(bar: Mapping[str, Any]) -> float | None:
    high, low, close = _num(bar.get("high")), _num(bar.get("low")), _num(bar.get("close"))
    if None in {high, low, close} or close <= 0:
        return None
    return (high - low) / close


def _features_for_code(
    bars: list[dict[str, Any]],
    minimum_history: int,
    unavailable: Counter[str] | None = None,
) -> list[dict[str, Any]]:
    unavailable = unavailable if unavailable is not None else Counter()
    output: list[dict[str, Any]] = []
    for index in range(minimum_history, len(bars) - 3):
        unavailable["windows_considered"] += 1
        current = bars[index]
        prior = bars[:index]
        current_values = {
            field: _num(current.get(field))
            for field in ("open", "high", "low", "close", "volume")
        }
        invalid_current = [
            field for field, value in current_values.items()
            if value is None or value <= 0
        ]
        if invalid_current:
            for field in invalid_current:
                unavailable[f"decision_{field}_missing_or_non_positive"] += 1
            continue
        close_window = [_num(row.get("close")) for row in prior[-65:]]
        volume_window = [_num(row.get("volume")) for row in prior[-20:]]
        if any(value is None or value <= 0 for value in close_window):
            unavailable["history_close_missing_or_non_positive"] += 1
            continue
        if any(value is None or value <= 0 for value in volume_window):
            unavailable["history_volume_missing_or_non_positive"] += 1
            continue
        # Convert only after the exact required windows passed validation.
        # Older rows outside these windows are irrelevant and may legitimately
        # have sparse provider fields; converting the entire history caused the
        # production ``float(None)`` failure.
        closes_f = [float(value) for value in close_window]
        volumes_f = [float(value) for value in volume_window]
        current_close = float(current_values["close"])
        current_open = float(current_values["open"])
        current_volume = float(current_values["volume"])
        high60 = max(closes_f[-60:])
        short_ranges = [_daily_range(row) for row in prior[-5:]]
        baseline_ranges = [_daily_range(row) for row in prior[-20:-5]]
        if any(value is None for value in short_ranges + baseline_ranges):
            unavailable["history_high_low_close_missing_or_non_positive"] += 1
            continue
        baseline_range = mean(float(value) for value in baseline_ranges)
        if baseline_range <= 0:
            unavailable["history_volatility_baseline_non_positive"] += 1
            continue
        avg_volume5 = mean(volumes_f[-5:])
        output.append({
            "code": current["code"],
            "decision_date": current["date"],
            "drawdown_60": 1.0 - current_close / high60,
            "volume_percentile_20": _percentile_rank(volumes_f[-20:], current_volume),
            "volatility_ratio": mean(float(value) for value in short_ranges) / baseline_range,
            "bullish_reversal": current_close > current_open and current_close >= closes_f[-1],
            "prior_return_5": closes_f[-1] / closes_f[-6] - 1.0,
            "gap": current_open / closes_f[-1] - 1.0,
            "volume_ratio_5": current_volume / avg_volume5,
            "return_20": current_close / closes_f[-20] - 1.0,
            "prior_high_10": max(closes_f[-10:]),
            "prior_high_20": max(closes_f[-20:]),
            "prior_high_30": max(closes_f[-30:]),
            "close": current_close,
            "open": current_open,
            "future_bars": {
                1: {"entry": bars[index + 1], "exit": bars[index + 1]},
                3: {"entry": bars[index + 1], "exit": bars[index + 3]},
            },
        })
        unavailable["windows_available"] += 1
    return output


def _regimes(
    benchmark: list[dict[str, Any]],
    policy: Mapping[str, Any],
    unavailable: Counter[str] | None = None,
) -> dict[str, str]:
    unavailable = unavailable if unavailable is not None else Counter()
    settings = policy["regime"]
    lookback = int(settings["lookback_sessions"])
    output: dict[str, str] = {}
    for index in range(lookback, len(benchmark)):
        close = _num(benchmark[index].get("close"))
        prior = _num(benchmark[index - lookback].get("close"))
        if close is None or prior is None or prior <= 0:
            unavailable["benchmark_regime_close_missing_or_non_positive"] += 1
            continue
        ret = close / prior - 1.0
        output[benchmark[index]["date"]] = (
            "up" if ret > float(settings["up_threshold"])
            else "down" if ret < float(settings["down_threshold"])
            else "range"
        )
        unavailable["benchmark_regime_sessions_available"] += 1
    return output


def _outcomes(
    feature: Mapping[str, Any],
    benchmark_by_date: Mapping[str, dict[str, Any]],
    policy: Mapping[str, Any],
    expected_sessions: Mapping[str, Mapping[int, str]] | None = None,
    unavailable: Counter[str] | None = None,
) -> dict[int, dict[str, float]]:
    unavailable = unavailable if unavailable is not None else Counter()
    result: dict[int, dict[str, float]] = {}
    costs = policy["cost_model"]
    for horizon in (1, 3):
        unavailable["outcomes_considered"] += 1
        future = feature["future_bars"][horizon]
        entry = future["entry"]
        exit_bar = future["exit"]
        entry_date, exit_date = entry["date"], exit_bar["date"]
        expected = (expected_sessions or {}).get(str(feature.get("decision_date") or ""), {})
        # A suspended/missing stock bar must not silently move entry to its
        # next available date.  Entry and exit have to be the benchmark's exact
        # first/horizon-th sessions after D, matching forward settlement.
        if not expected or 1 not in expected or horizon not in expected:
            unavailable["benchmark_expected_session_missing"] += 1
            continue
        if (
            entry_date != expected.get(1) or exit_date != expected.get(horizon)
        ):
            unavailable["stock_session_mismatch_or_suspension"] += 1
            continue
        benchmark_entry = benchmark_by_date.get(entry_date)
        benchmark_exit = benchmark_by_date.get(exit_date)
        if benchmark_entry is None or benchmark_exit is None:
            unavailable["benchmark_bar_missing"] += 1
            continue
        entry_open, exit_close = _num(entry.get("open")), _num(exit_bar.get("close"))
        bench_open = _num(benchmark_entry.get("open"))
        bench_close = _num(benchmark_exit.get("close"))
        prices = {
            "stock_entry_open": entry_open,
            "stock_exit_close": exit_close,
            "benchmark_entry_open": bench_open,
            "benchmark_exit_close": bench_close,
        }
        invalid_prices = [
            name for name, value in prices.items()
            if value is None or value <= 0
        ]
        if invalid_prices:
            for name in invalid_prices:
                unavailable[f"{name}_missing_or_non_positive"] += 1
            continue
        buy_rate = float(costs["commission_rate"]) + float(costs["entry_slippage_bps"]) / 10000
        sell_rate = float(costs["commission_rate"]) + float(costs["stamp_tax_rate"]) + float(costs["exit_slippage_bps"]) / 10000
        net = float(exit_close) * (1.0 - sell_rate) / (float(entry_open) * (1.0 + buy_rate)) - 1.0
        benchmark_return = float(bench_close) / float(bench_open) - 1.0
        result[horizon] = {
            "net_return": net,
            "benchmark_return": benchmark_return,
            "excess_return": net - benchmark_return,
        }
        unavailable["outcomes_available"] += 1
    return result


def _cross_section_quantiles(features: Sequence[dict[str, Any]]) -> None:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in features:
        by_date[row["decision_date"]].append(row)
    for rows in by_date.values():
        ordered = sorted(rows, key=lambda row: (row["return_20"], row["code"]))
        size = len(ordered)
        for rank, row in enumerate(ordered, start=1):
            row["return_20_quantile"] = rank / size


def _signal(strategy_id: str, feature: Mapping[str, Any], params: Mapping[str, Any]) -> bool:
    if strategy_id == "reverse_volume_daily_proxy":
        ratio = _num(feature.get("volatility_ratio"))
        return bool(
            ratio is not None
            and float(params["drawdown_min"]) <= feature["drawdown_60"] <= float(params["drawdown_max"])
            and feature["volume_percentile_20"] <= float(params["volume_percentile_max"])
            and ratio <= float(params["volatility_ratio_max"])
            and feature["bullish_reversal"]
        )
    if strategy_id == "rank_surprise_daily_proxy":
        return bool(
            feature["prior_return_5"] <= float(params["prior_return_max"])
            and feature["gap"] >= float(params["gap_min"])
            and feature["volume_ratio_5"] >= float(params["volume_ratio_min"])
            and feature["close"] > feature["open"]
        )
    if strategy_id == "assist_strength_daily_proxy":
        lookback = int(params["breakout_lookback"])
        return bool(
            feature.get("return_20_quantile", 0.0) >= float(params["relative_strength_quantile"])
            and feature["close"] > feature[f"prior_high_{lookback}"]
            and feature["volume_ratio_5"] >= float(params["volume_ratio_min"])
        )
    raise ValueError(f"strategy not allowed: {strategy_id}")


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"sample_count": 0, "mean_net_return": None, "sum_net_return": None, "mean_excess_return": None, "sum_excess_return": None, "hit_rate": None, "median_net_return": None}
    net = [float(row["net_return"]) for row in rows]
    excess = [float(row["excess_return"]) for row in rows]
    return {
        "sample_count": len(rows),
        "mean_net_return": round(mean(net), 8),
        "sum_net_return": round(sum(net), 8),
        "median_net_return": round(median(net), 8),
        "mean_excess_return": round(mean(excess), 8),
        "sum_excess_return": round(sum(excess), 8),
        "hit_rate": round(sum(value > 0 for value in net) / len(net), 8),
    }


def _score_variant(features: Sequence[dict[str, Any]], strategy_id: str, params: Mapping[str, Any], horizon: int, dates: set[str]) -> tuple[float, int]:
    rows = [row["outcomes"][horizon] for row in features if row["decision_date"] in dates and horizon in row["outcomes"] and _signal(strategy_id, row, params)]
    return (mean(item["excess_return"] for item in rows), len(rows)) if rows else (-math.inf, 0)


def run(rows: Iterable[Mapping[str, Any]], policy: Mapping[str, Any]) -> dict[str, Any]:
    """Run point-in-time proxy research without mutating any runtime state."""
    grouped = _normalise_bars(rows)
    benchmark_code = str(policy["benchmark"]["code"]).zfill(6)
    benchmark = grouped.get(benchmark_code) or []
    if not benchmark:
        raise ValueError("CSI300 benchmark bars are required")
    dates = [row["date"] for row in benchmark]
    benchmark_by_date = {row["date"]: row for row in benchmark}
    expected_sessions = {
        date: {
            horizon: dates[index + horizon]
            for horizon in (1, 3)
            if index + horizon < len(dates)
        }
        for index, date in enumerate(dates)
    }
    source_counts = Counter(
        str(row.get("source") or "unknown")
        for code_rows in grouped.values()
        for row in code_rows
    )
    minimum_history = int(policy["minimum_history_bars"])
    equity_codes = sorted(code for code in grouped if code != benchmark_code)
    feature_coverage: Counter[str] = Counter()
    code_unavailable_reasons: Counter[str] = Counter()
    features: list[dict[str, Any]] = []
    codes_with_features: set[str] = set()
    for code in equity_codes:
        local_coverage: Counter[str] = Counter()
        code_features = _features_for_code(
            grouped[code], minimum_history, local_coverage
        )
        feature_coverage.update(local_coverage)
        if code_features:
            codes_with_features.add(code)
            features.extend(code_features)
        else:
            reasons = [
                reason for reason, count in local_coverage.items()
                if count and reason not in {"windows_considered", "windows_available"}
            ]
            if not local_coverage.get("windows_considered"):
                code_unavailable_reasons["history_shorter_than_feature_and_t3_window"] += 1
            else:
                for reason in reasons or ["all_decision_windows_unavailable"]:
                    code_unavailable_reasons[reason] += 1
    # The feature rows retain only the two future snapshots needed by the
    # research label.  Releasing the full per-code history keeps a full-market
    # run bounded instead of retaining both the SQLite rows and feature table.
    grouped.clear()
    _cross_section_quantiles(features)
    benchmark_coverage: Counter[str] = Counter()
    regimes = _regimes(benchmark, policy, benchmark_coverage)
    outcome_coverage: Counter[str] = Counter()
    for row in features:
        row["regime"] = regimes.get(row["decision_date"], "unavailable")
        row["outcomes"] = _outcomes(
            row, benchmark_by_date, policy, expected_sessions, outcome_coverage
        )

    wf = policy["walk_forward"]
    try:
        folds = build_walk_forward_folds(len(dates), **{key: wf[key] for key in ("train_size", "calibration_size", "test_size", "step", "purge", "embargo", "mode")})
    except ValidationError as exc:
        raise ValueError(f"insufficient sessions for walk-forward: {exc.code}") from exc

    strategy_reports: dict[str, Any] = {}
    for strategy_id in ALLOWED_STRATEGIES:
        definition = policy["strategies"][strategy_id]
        variants = list(definition["variants"])
        horizon = int(definition["primary_horizon"])
        selected: list[str] = []
        test_rows: list[dict[str, Any]] = []
        fold_reports: list[dict[str, Any]] = []
        for fold in folds:
            train_dates = set(dates[fold["train_start"]:fold["train_end"]])
            calibration_dates = set(dates[fold["calibration_start"]:fold["calibration_end"]])
            test_dates = set(dates[fold["test_start"]:fold["test_end"]])
            scores = []
            for variant in variants:
                train_score, train_n = _score_variant(features, strategy_id, variant, horizon, train_dates)
                calibration_score, calibration_n = _score_variant(features, strategy_id, variant, horizon, calibration_dates)
                minimum = int(wf["minimum_selection_samples"])
                eligible = train_n >= minimum and calibration_n >= max(2, minimum // 2)
                scores.append({"variant_id": variant["id"], "train_excess": None if not math.isfinite(train_score) else train_score, "train_n": train_n, "calibration_excess": None if not math.isfinite(calibration_score) else calibration_score, "calibration_n": calibration_n, "eligible": eligible, "params": dict(variant)})
            eligible_scores = [score for score in scores if score["eligible"]]
            if not eligible_scores:
                fold_reports.append({"fold_id": fold["fold_id"], "status": "not_evaluated", "reason": "selection_samples_insufficient", "variants": scores})
                continue
            winner = max(eligible_scores, key=lambda score: (score["calibration_excess"], score["train_excess"], score["variant_id"]))
            selected.append(winner["variant_id"])
            params = winner["params"]
            fold_rows = []
            for feature in features:
                if feature["decision_date"] not in test_dates or horizon not in feature["outcomes"] or not _signal(strategy_id, feature, params):
                    continue
                outcome = feature["outcomes"][horizon]
                record = {"code": feature["code"], "decision_date": feature["decision_date"], "regime": feature["regime"], "variant_id": winner["variant_id"], **outcome}
                fold_rows.append(record)
                test_rows.append(record)
            fold_reports.append({"fold_id": fold["fold_id"], "status": "evaluated", "selected_variant": winner["variant_id"], "test": _summary(fold_rows), "variants": scores})
        counts = Counter(selected)
        modal_count = max(counts.values(), default=0)
        selection_shares = {key: round(value / len(selected), 8) for key, value in sorted(counts.items())} if selected else {}
        adjacent = sum(left == right for left, right in zip(selected, selected[1:]))
        stability = {
            "evaluated_fold_count": len(selected),
            "selection_frequency": selection_shares,
            "modal_share": round(modal_count / len(selected), 8) if selected else None,
            "adjacent_selection_consistency": round(adjacent / (len(selected) - 1), 8) if len(selected) > 1 else None,
            "fold_excess_std": round(pstdev([row["test"]["mean_excess_return"] for row in fold_reports if row.get("test", {}).get("mean_excess_return") is not None]), 8) if sum(row.get("test", {}).get("mean_excess_return") is not None for row in fold_reports) > 1 else None,
        }
        by_regime = {regime: _summary([row for row in test_rows if row["regime"] == regime]) for regime in ("up", "range", "down", "unavailable")}
        strategy_reports[strategy_id] = {
            "source_strategy": definition["source_strategy"],
            "hypothesis_scope": "observable_daily_price_volume_sub_hypothesis_only",
            "primary_horizon": horizon,
            "overall": _summary(test_rows),
            "by_regime": by_regime,
            "parameter_stability": stability,
            "folds": fold_reports,
        }
    qualification = dict(policy["qualification"])
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "engine_version": ENGINE_VERSION,
        "qualification": qualification,
        "claims": {
            "complete_strategy_validation": False,
            "included": ["S1 daily close-observable weak-to-strong proxy", "S3 full-market daily relative-strength proxy", "S5 daily drawdown/contraction/exhaustion proxy"],
            "excluded": ["S1 auction and 09:45 volume ratio", "S2 same-clock minute turnover", "S3 sector leader and intraday breakout", "S4 pretable and reaction timing", "S5 prior leader, sentiment and directional minute-volume confirmation"],
        },
        "point_in_time": {"signal_cutoff": "decision_session_close", "entry_rule": "next_trading_session_open", "horizons": [1, 3], "benchmark": dict(policy["benchmark"]), "purge_sessions": int(wf["purge"]), "embargo_sessions": int(wf["embargo"])},
        "session_count": len(dates),
        "feature_row_count": len(features),
        "fold_count": len(folds),
        "priority_order": ["reverse_volume_daily_proxy", "rank_surprise_daily_proxy", "assist_strength_daily_proxy"],
        "input_provenance": {
            "minimum_date": min(dates),
            "maximum_date": max(dates),
            "source_row_counts": dict(sorted(source_counts.items())),
            "adjust_flag": policy.get("adjust_flag"),
        },
        "coverage": {
            "equity_code_count": len(equity_codes),
            "codes_with_features": len(codes_with_features),
            "codes_without_features": len(equity_codes) - len(codes_with_features),
            "code_unavailable_reason_counts": dict(sorted(code_unavailable_reasons.items())),
            "decision_windows_considered": int(feature_coverage["windows_considered"]),
            "decision_windows_available": int(feature_coverage["windows_available"]),
            "decision_window_coverage_ratio": round(
                feature_coverage["windows_available"] / feature_coverage["windows_considered"], 8
            ) if feature_coverage["windows_considered"] else 0.0,
            "decision_window_unavailable_reason_counts": dict(sorted(
                (reason, count) for reason, count in feature_coverage.items()
                if reason not in {"windows_considered", "windows_available"}
            )),
            "outcomes_considered": int(outcome_coverage["outcomes_considered"]),
            "outcomes_available": int(outcome_coverage["outcomes_available"]),
            "outcome_coverage_ratio": round(
                outcome_coverage["outcomes_available"] / outcome_coverage["outcomes_considered"], 8
            ) if outcome_coverage["outcomes_considered"] else 0.0,
            "outcome_unavailable_reason_counts": dict(sorted(
                (reason, count) for reason, count in outcome_coverage.items()
                if reason not in {"outcomes_considered", "outcomes_available"}
            )),
            "benchmark_regime_sessions_available": int(
                benchmark_coverage["benchmark_regime_sessions_available"]
            ),
            "benchmark_unavailable_reason_counts": dict(sorted(
                (reason, count) for reason, count in benchmark_coverage.items()
                if reason != "benchmark_regime_sessions_available"
            )),
        },
        "strategies": strategy_reports,
    }
    report["report_sha256"] = _hash(report)
    return report


__all__ = ["ALLOWED_STRATEGIES", "ENGINE_VERSION", "SCHEMA", "load_policy", "run"]
