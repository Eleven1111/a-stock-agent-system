"""Cache-only, non-live Bayesian shadow evaluation for four-dimension weights."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

import four_dim_score_log as observation_contract
import local_market_history
from config_registry import config_path
from execution_model import FEE_SCHEDULE, net_return_pct

DIMENSIONS = ("technical", "sentiment", "catalyst", "deep")
BENCHMARK_CODE = "000300"
MIN_FIT_DAYS = 60
MIN_OOS_DAYS = 60
ASSUMED_NOTIONAL = 20_000.0


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_hash(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _history_rows(codes: Sequence[str]) -> dict[str, dict[str, dict[str, Any]]]:
    unique = list(dict.fromkeys([*codes, BENCHMARK_CODE]))
    rows = local_market_history.get_daily_bars(unique, "9999-12-31", 5000, adjust_flag="qfq")
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["code"]), {})[str(row["trading_date"])] = dict(row)
    return grouped


def _incomplete_label(reason: str) -> dict[str, Any]:
    return {"schema": "four_dim_forward_label_v1", "status": "incomplete", "reason": reason}


def _return(start: float, end: float) -> float:
    return round((end / start - 1.0) * 100.0, 6)


def _one_label(
    observation: Mapping[str, Any],
    history: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    available_asof: str | None = None,
    assumed_notional: float = ASSUMED_NOTIONAL,
) -> dict[str, Any]:
    code = str(observation.get("code") or "").zfill(6)
    asof = str(observation.get("trading_date") or "")
    benchmark = history.get(BENCHMARK_CODE, {})
    dates = sorted(benchmark)
    if asof not in benchmark:
        return _incomplete_label("benchmark_signal_day_missing")
    index = dates.index(asof)
    if index + 3 >= len(dates):
        return _incomplete_label("t3_not_available")
    t1_date, t3_date = dates[index + 1], dates[index + 3]
    if available_asof is not None and t3_date > available_asof:
        return _incomplete_label("label_not_available_asof")
    stock = history.get(code, {})
    required = (asof, t1_date, t3_date)
    if any(day not in stock for day in required):
        return _incomplete_label("stock_bar_missing_or_suspended")
    stock_close = [_finite(stock[day].get("close")) for day in required]
    bench_close = [_finite(benchmark[day].get("close")) for day in required]
    if any(value is None or value <= 0 for value in [*stock_close, *bench_close]):
        return _incomplete_label("close_invalid")
    s0, s1, s3 = stock_close
    b0, b1, b3 = bench_close
    t1_gross, t3_gross = _return(s0, s1), _return(s0, s3)
    t1_bench, t3_bench = _return(b0, b1), _return(b0, b3)
    try:
        t1_net = net_return_pct(gross_return_pct=t1_gross, notional=assumed_notional, asof=asof)
        t3_net = net_return_pct(gross_return_pct=t3_gross, notional=assumed_notional, asof=asof)
    except ValueError as exc:
        return _incomplete_label(str(exc))
    evidence = [stock[day] for day in required] + [benchmark[day] for day in required]
    return {
        "schema": "four_dim_forward_label_v1",
        "status": "complete",
        "observation_id": observation.get("observation_id"),
        "code": code,
        "trading_date": asof,
        "t1_date": t1_date,
        "t3_date": t3_date,
        "t1_gross_return_pct": t1_gross,
        "t3_gross_return_pct": t3_gross,
        "t1_benchmark_return_pct": t1_bench,
        "t3_benchmark_return_pct": t3_bench,
        "t1_net_return_pct": t1_net["net_return_pct"],
        "t3_net_return_pct": t3_net["net_return_pct"],
        "t1_net_excess_pct": round(t1_net["net_return_pct"] - t1_bench, 6),
        "t3_net_excess_pct": round(t3_net["net_return_pct"] - t3_bench, 6),
        "assumed_notional": assumed_notional,
        "fee_schedule_version": t1_net["fee_schedule_version"],
        "label_available_at": f"{t3_date}T15:00:00+08:00",
        "history_sha256": _stable_hash(evidence),
    }


def build_labels(
    observations: Sequence[Mapping[str, Any]], *, asof: str | None = None,
    assumed_notional: float = ASSUMED_NOTIONAL,
) -> dict[str, dict[str, Any]]:
    """Build T+1/T+3 benchmark- and cost-adjusted labels from SQLite only."""
    codes = [str(row.get("code") or "").zfill(6) for row in observations if row.get("code")]
    if not codes:
        return {}
    history = _history_rows(codes)
    return {
        str(row["observation_id"]): _one_label(
            row, history, available_asof=asof, assumed_notional=assumed_notional,
        )
        for row in observations if row.get("observation_id")
    }


def _is_digest(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text.lower())


def _valid_weight_map(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != set(DIMENSIONS):
        return False
    weights = [_finite(value.get(dim)) for dim in DIMENSIONS]
    return (
        all(weight is not None and weight >= 0 for weight in weights)
        and math.isclose(sum(float(weight) for weight in weights), 1.0, abs_tol=1e-6)
    )


def _validation_reason(
    observation: Mapping[str, Any], label: Mapping[str, Any], *, lane: str, asof: str,
) -> str | None:
    identifier = str(observation.get("observation_id") or "")
    trading_date = str(observation.get("trading_date") or "")
    if observation.get("schema") != "four_dim_observation_v2":
        return "schema"
    if not identifier:
        return "observation_id"
    if observation.get("strategy_lane") != lane:
        return "other_lane"
    if observation.get("live_effect") != "none" or observation.get("research_only") is not True:
        return "not_research_only"
    try:
        if date.fromisoformat(trading_date) > date.fromisoformat(asof):
            return "observation_after_asof"
    except ValueError:
        return "trading_date"
    if (observation.get("point_in_time") or {}).get("status") != "complete":
        return "point_in_time"
    snapshot = observation.get("input_snapshot") or {}
    snapshot_ref = str(snapshot.get("ref") or "")
    if not snapshot_ref or Path(snapshot_ref).name == "candidate_pool_latest.json":
        return "mutable_snapshot"
    if not _is_digest(snapshot.get("sha256")):
        return "snapshot_hash"
    versions = observation.get("versions") or {}
    if any(not _is_digest(versions.get(key)) for key in ("scorer_sha256", "config_sha256", "contract_sha256")):
        return "version_hash"
    if not _is_digest(observation.get("input_fingerprint_sha256")) or not _is_digest(observation.get("input_bundle_sha256")):
        return "input_hash"
    if observation_contract.recompute_input_bundle_sha256(observation) != observation.get("input_bundle_sha256"):
        return "input_bundle_integrity"
    if observation_contract.recompute_observation_id(observation) != identifier:
        return "observation_id_integrity"
    if not _valid_weight_map(observation.get("current_weights")):
        return "current_weights"
    if not _valid_weight_map(observation.get("effective_weights")):
        return "effective_weights"
    dimensions = observation.get("dimensions") or {}
    for dim in DIMENSIONS:
        payload = dimensions.get(dim) if isinstance(dimensions, Mapping) else None
        if not isinstance(payload, Mapping) or payload.get("status") != "available":
            return f"{dim}_status"
        if _finite(payload.get("score")) is None:
            return f"{dim}_score"
        if str(payload.get("source") or "") in {"", "unknown", "unavailable"}:
            return f"{dim}_source"
        source_asof = str(payload.get("asof") or "")[:10]
        try:
            if date.fromisoformat(source_asof) > date.fromisoformat(trading_date):
                return f"{dim}_future"
        except ValueError:
            return f"{dim}_asof"
    if label.get("status") != "complete":
        return f"label_{label.get('reason') or 'incomplete'}"
    if _finite(label.get("t1_net_excess_pct")) is None or _finite(label.get("t3_net_excess_pct")) is None:
        return "label_return"
    if str(label.get("t3_date") or "9999-12-31") > asof:
        return "label_after_asof"
    return None


def _valid_rows(
    observations: Sequence[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]],
    lane: str, *, asof: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    attrition: Counter[str] = Counter()
    for observation in observations:
        identifier = str(observation.get("observation_id") or "")
        if identifier in seen:
            attrition["duplicate_observation_id"] += 1
            continue
        reason = _validation_reason(observation, labels.get(identifier) or {}, lane=lane, asof=asof)
        if reason is not None:
            attrition[reason] += 1
            continue
        dimensions = observation["dimensions"]
        rows.append({
            "observation_id": identifier,
            "trading_date": str(observation["trading_date"]),
            "scores": [float(dimensions[dim]["score"]) for dim in DIMENSIONS],
            "t1": float(labels[identifier]["t1_net_excess_pct"]),
            "t3": float(labels[identifier]["t3_net_excess_pct"]),
        })
        seen.add(identifier)
    return (
        sorted(rows, key=lambda row: (row["trading_date"], row["observation_id"])),
        dict(sorted(attrition.items())),
    )


def _weights(value: Mapping[str, Any]) -> np.ndarray:
    array = np.array([max(0.0, float(value.get(dim, 0.0))) for dim in DIMENSIONS], dtype=float)
    total = float(array.sum())
    return array / total if total > 0 else np.full(len(DIMENSIONS), 1.0 / len(DIMENSIONS))


def _weighted_quantile(values: np.ndarray, probabilities: np.ndarray, quantile: float) -> float:
    order = np.argsort(values)
    ordered_values, ordered_probabilities = values[order], probabilities[order]
    cumulative = np.cumsum(ordered_probabilities)
    return float(np.interp(quantile, cumulative, ordered_values))


def _posterior(rows: Sequence[Mapping[str, Any]], prior: np.ndarray, draws: int, seed: int) -> dict[str, Any]:
    x = np.asarray([row["scores"] for row in rows], dtype=float)
    y = np.asarray([row["t3"] for row in rows], dtype=float)
    rng = np.random.default_rng(seed)
    samples = rng.dirichlet(np.maximum(prior * 24.0, 0.25), size=max(128, draws))
    samples = np.vstack([prior, samples])
    composites = x @ samples.T
    centered_x = composites - composites.mean(axis=0)
    centered_y = y - y.mean()
    denominator = np.sum(centered_x * centered_x, axis=0)
    slopes = np.divide(centered_x.T @ centered_y, denominator, out=np.zeros_like(denominator), where=denominator > 0)
    intercepts = y.mean() - slopes * composites.mean(axis=0)
    residuals = y[:, None] - (intercepts[None, :] + composites * slopes[None, :])
    scale = np.median(np.abs(residuals), axis=0) * 1.4826
    scale = np.maximum(scale, 0.05)
    nu = 4.0
    log_likelihood = np.sum(-np.log(scale)[None, :] - ((nu + 1.0) / 2.0) * np.log1p((residuals / scale) ** 2 / nu), axis=0)
    log_likelihood -= np.max(log_likelihood)
    probabilities = np.exp(log_likelihood)
    probabilities /= probabilities.sum()
    means = probabilities @ samples
    means /= means.sum()
    intervals = {}
    for index, dim in enumerate(DIMENSIONS):
        mean = float(means[index])
        lower = _weighted_quantile(samples[:, index], probabilities, 0.05)
        upper = _weighted_quantile(samples[:, index], probabilities, 0.95)
        intervals[dim] = {
            "mean": round(mean, 6),
            "lower_90": round(min(lower, mean), 6),
            "upper_90": round(max(upper, mean), 6),
        }
    return {
        "method": "dirichlet_simplex_student_t_importance_shadow_v1",
        "primary_outcome": "t3_net_excess_pct",
        "limitations": [
            "not_a_hierarchical_bayesian_model",
            "slope_intercept_and_scale_are_profile_estimates",
            "t1_is_evaluation_only",
        ],
        "student_t_df": 4,
        "prior_concentration": 24.0,
        "draws": int(samples.shape[0]),
        "weights": intervals,
        "mean_vector": {dim: round(float(means[index]), 6) for index, dim in enumerate(DIMENSIONS)},
    }


def _metric(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"sample_days": 0, "mean_pct": None, "median_pct": None, "win_rate": None, "max_drawdown_pct": None}
    array = np.asarray(values, dtype=float)
    wealth = np.cumprod(1.0 + array / 100.0)
    peaks = np.maximum.accumulate(np.concatenate(([1.0], wealth)))[1:]
    drawdown = (wealth / peaks - 1.0) * 100.0
    return {
        "sample_days": len(values),
        "mean_pct": round(float(array.mean()), 6),
        "median_pct": round(float(np.median(array)), 6),
        "win_rate": round(float(np.mean(array > 0)), 6),
        "max_drawdown_pct": round(float(drawdown.min()), 6),
    }


def _evaluate(rows: Sequence[Mapping[str, Any]], weights: np.ndarray) -> dict[str, Any]:
    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(str(row["trading_date"]), []).append(row)
    t1, t3 = [], []
    for day_rows in by_day.values():
        ranked = sorted(day_rows, key=lambda row: float(np.dot(row["scores"], weights)), reverse=True)
        selected = ranked[:max(1, math.ceil(len(ranked) * 0.2))]
        t1.append(float(np.mean([row["t1"] for row in selected])))
        t3.append(float(np.mean([row["t3"] for row in selected])))
    return {"t1_net_excess": _metric(t1), "t3_net_excess": _metric(t3)}


def _comparison(rows: Sequence[Mapping[str, Any]], current: np.ndarray, shadow: np.ndarray) -> dict[str, Any]:
    equal = np.full(len(DIMENSIONS), 1.0 / len(DIMENSIONS))
    ablation = {}
    for index, dim in enumerate(DIMENSIONS):
        candidate = current.copy()
        candidate[index] = 0.0
        candidate /= candidate.sum()
        ablation[f"without_{dim}"] = _evaluate(rows, candidate)
    return {
        "current": _evaluate(rows, current),
        "equal": _evaluate(rows, equal),
        "shadow": _evaluate(rows, shadow),
        "ablation": ablation,
    }


def _new_freeze(
    rows: Sequence[Mapping[str, Any]], lane: str, current: np.ndarray, *,
    min_fit_days: int, posterior_draws: int, seed: int,
) -> dict[str, Any]:
    fit_dates = sorted({row["trading_date"] for row in rows})[:min_fit_days]
    fit_date_set = set(fit_dates)
    fit_rows = [row for row in rows if row["trading_date"] in fit_date_set]
    posterior = _posterior(fit_rows, current, posterior_draws, seed)
    payload = {
        "schema": "four_dim_weight_fit_freeze_v1",
        "lane": lane,
        "fit_cutoff": fit_dates[-1],
        "fit_dates": fit_dates,
        "fit_trading_days": len(fit_dates),
        "fit_observation_ids": [row["observation_id"] for row in fit_rows],
        "training_set_sha256": _stable_hash(fit_rows),
        "current_weights": {
            dim: round(float(current[index]), 6) for index, dim in enumerate(DIMENSIONS)
        },
        "posterior": posterior,
        "shadow_weights": posterior["mean_vector"],
        "method": "dirichlet_simplex_student_t_importance_shadow_v1",
        "research_code_sha256": _file_hash(__file__),
        "scoring_config_sha256": _file_hash(config_path("scoring")),
        "fee_schedule_sha256": _stable_hash(FEE_SCHEDULE),
        "research_only": True,
        "live_effect": "none",
    }
    payload["model_sha256"] = _stable_hash(payload)
    return payload


def _valid_freeze(value: Any, lane: str, min_fit_days: int) -> bool:
    if not isinstance(value, Mapping):
        return False
    frozen_payload = dict(value)
    model_sha = frozen_payload.pop("model_sha256", None)
    return (
        value.get("schema") == "four_dim_weight_fit_freeze_v1"
        and value.get("lane") == lane
        and value.get("live_effect") == "none"
        and int(value.get("fit_trading_days") or 0) == min_fit_days
        and bool(value.get("fit_cutoff"))
        and _is_digest(value.get("training_set_sha256"))
        and value.get("method") == "dirichlet_simplex_student_t_importance_shadow_v1"
        and value.get("research_code_sha256") == _file_hash(__file__)
        and value.get("scoring_config_sha256") == _file_hash(config_path("scoring"))
        and value.get("fee_schedule_sha256") == _stable_hash(FEE_SCHEDULE)
        and _is_digest(model_sha)
        and _stable_hash(frozen_payload) == model_sha
        and _valid_weight_map(value.get("current_weights"))
        and _valid_weight_map(value.get("shadow_weights"))
        and isinstance(value.get("posterior"), Mapping)
    )


def _lane_report(
    rows: Sequence[Mapping[str, Any]], lane: str, current_map: Mapping[str, Any],
    *, min_fit_days: int, min_oos_days: int, posterior_draws: int, seed: int,
    frozen: Mapping[str, Any] | None, attrition: Mapping[str, int],
) -> dict[str, Any]:
    dates = sorted({row["trading_date"] for row in rows})
    base = {
        "lane": lane,
        "valid_rows": len(rows),
        "valid_trading_days": len(dates),
        "attrition": dict(attrition),
        "research_only": True,
        "live_effect": "none",
        "stopping_conditions": {
            "minimum_fit_days": min_fit_days,
            "minimum_unseen_oos_days": min_oos_days,
            "point_in_time_required": True,
            "version_hashes_required": True,
            "manual_approval_required": True,
        },
    }
    current = _weights(current_map)
    freeze = dict(frozen) if frozen is not None else None
    if freeze is not None and not _valid_freeze(freeze, lane, min_fit_days):
        return {**base, "status": "invalid_frozen_model", "posterior": None, "comparison": None, "freeze": None}
    if not dates:
        return {
            **base,
            "status": "frozen_model_no_valid_observations" if freeze else "no_valid_observations",
            "posterior": freeze.get("posterior") if freeze else None,
            "comparison": None,
            "freeze": freeze,
        }
    if freeze is None:
        if len(dates) < min_fit_days:
            return {**base, "status": "insufficient_training_days", "posterior": None, "comparison": None, "freeze": None}
        freeze = _new_freeze(
            rows, lane, current,
            min_fit_days=min_fit_days, posterior_draws=posterior_draws, seed=seed,
        )
    fit_cutoff = str(freeze["fit_cutoff"])
    oos_dates_all = [day for day in dates if day > fit_cutoff]
    comparison_dates = oos_dates_all[:min_oos_days]
    comparison_date_set = set(comparison_dates)
    compare_rows = [row for row in rows if row["trading_date"] in comparison_date_set]
    shadow = _weights(freeze["shadow_weights"])
    oos_complete = len(comparison_dates) >= min_oos_days
    status = "oos_complete_research_only" if oos_complete else "oos_pending_research_only"
    return {
        **base,
        "status": status,
        "fit_trading_days": freeze["fit_trading_days"],
        "fit_cutoff": fit_cutoff,
        "model_sha256": freeze["model_sha256"],
        "comparison_dates": len(comparison_dates),
        "comparison_date_values": comparison_dates,
        "comparison_scope": "frozen_forward_oos",
        "current_weights": freeze["current_weights"],
        "posterior": freeze["posterior"],
        "comparison": _comparison(compare_rows, _weights(freeze["current_weights"]), shadow) if compare_rows else None,
        "freeze": freeze,
        "rollback": {
            "action": "no_live_change",
            "authoritative_config": "config/scoring.yaml",
            "restore_weights": {dim: round(float(current[index]), 6) for index, dim in enumerate(DIMENSIONS)},
        },
    }


def build_shadow_report(
    observations: Sequence[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]], *,
    current_weights: Mapping[str, Mapping[str, Any]], min_fit_days: int = MIN_FIT_DAYS,
    min_oos_days: int = MIN_OOS_DAYS, posterior_draws: int = 4096, seed: int = 303,
    asof: str = "9999-12-31", frozen_lanes: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Freeze the first fit window, then evaluate only later as-of-safe dates."""
    lanes = {}
    for index, lane in enumerate(("trend", "daban")):
        rows, attrition = _valid_rows(observations, labels, lane, asof=asof)
        lanes[lane] = _lane_report(
            rows, lane, current_weights.get(lane) or {},
            min_fit_days=min_fit_days, min_oos_days=min_oos_days,
            posterior_draws=posterior_draws, seed=seed + index,
            frozen=(frozen_lanes or {}).get(lane), attrition=attrition,
        )
    complete = all(item["status"] == "oos_complete_research_only" for item in lanes.values())
    return {
        "schema": "four_dim_weight_shadow_report_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asof": asof,
        "status": "oos_complete_research_only" if complete else "insufficient_data",
        "research_only": True,
        "live_effect": "none",
        "lanes": lanes,
        "frozen_lanes": {
            lane: payload["freeze"] for lane, payload in lanes.items() if payload.get("freeze")
        },
        "methodology": {
            "method": "dirichlet_simplex_student_t_importance_shadow_v1",
            "primary_outcome": "t3_net_excess_pct",
            "t1_role": "evaluation_only",
            "hierarchical_bayesian": False,
        },
        "promotion": {
            "allowed": False,
            "reason": "shadow_only_manual_approval_required" if complete else "data_gate_not_met",
            "automatic_config_update": False,
        },
        "version_hashes": {
            "observation_set_sha256": _stable_hash(list(observations)),
            "label_set_sha256": _stable_hash(labels),
            "research_code_sha256": _file_hash(__file__),
            "scoring_config_sha256": _file_hash(config_path("scoring")),
            "fee_schedule_sha256": _stable_hash(FEE_SCHEDULE),
        },
    }
