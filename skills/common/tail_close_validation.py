"""Validation and promotion gates for isolated tail-close strategy families."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from a_share_rules import CalendarCoverageError, is_trading_day
from tail_close_strategy import (
    AFTER_HOURS_STRATEGY_ID,
    PRIMARY_STRATEGY_ID,
    canonical_hash,
)
from validation_program import (
    ValidationError,
    _locked_jsonl,
    _read_locked_records,
    compute_effective_samples,
    compute_statistical_validation,
    verify_oos_precommit_record,
)


SCHEMA = "tail_close_validation_gate_v1"
STRATEGY_FAMILIES = {
    PRIMARY_STRATEGY_ID: "continuous_auction",
    AFTER_HOURS_STRATEGY_ID: "after_hours_fixed_price",
}


def strategy_family_config_hash(
    strategy_config: Mapping[str, Any],
    strategy_id: str,
) -> str:
    if strategy_id not in STRATEGY_FAMILIES:
        raise TailCloseValidationError("strategy_family_unknown")
    common = {
        "schema": strategy_config.get("schema"),
        "version": strategy_config.get("version"),
        "strategy": (strategy_config.get("strategies") or {}).get(strategy_id),
        "runtime": strategy_config.get("runtime"),
        "execution": strategy_config.get("execution"),
        "exit": strategy_config.get("exit"),
        "portfolio": strategy_config.get("portfolio"),
        "safety": strategy_config.get("safety"),
    }
    if strategy_id == PRIMARY_STRATEGY_ID:
        common.update(
            {
                "universe": strategy_config.get("universe"),
                "market_gate": strategy_config.get("market_gate"),
                "sector_gate": strategy_config.get("sector_gate"),
                "stock_gate": strategy_config.get("stock_gate"),
                "ranking": strategy_config.get("ranking"),
                "validation": {
                    key: (strategy_config.get("validation") or {}).get(key)
                    for key in ("precommit", "oos", "shadow", "stopping_rules")
                },
            }
        )
    else:
        common["validation"] = {
            key: (strategy_config.get("validation") or {}).get(key)
            for key in ("after_hours_sibling", "stopping_rules")
        }
    return canonical_hash(common)


class TailCloseValidationError(ValueError):
    """Raised when an OOS payload violates the precommit."""


def _artifact_valid(payload: Mapping[str, Any], schema: str) -> bool:
    if payload.get("schema") != schema:
        return False
    expected = payload.get("artifact_hash")
    core = {
        key: value
        for key, value in payload.items()
        if key != "artifact_hash"
    }
    return isinstance(expected, str) and expected == canonical_hash(core)


def _normal_mean_p_value(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 1.0
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values)
    if stdev <= 0:
        return 0.0 if mean > 0 else 1.0
    z_score = mean / (stdev / math.sqrt(len(values)))
    return 0.5 * math.erfc(z_score / math.sqrt(2))


def _profit_factor(values: Sequence[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses == 0:
        return float("inf") if gains > 0 else None
    return gains / losses


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_registered_oos_evidence(
    *,
    registry_path: str | Path,
    precommit_id: str,
    reveal_record_sha256: str,
    dataset_path: str | Path,
    outcomes: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve immutable OOS evidence from the shared append-only registry."""
    persisted_registry = Path(registry_path)
    if not persisted_registry.is_file():
        raise TailCloseValidationError("oos_registry_invalid")
    try:
        with _locked_jsonl(persisted_registry) as handle:
            records = _read_locked_records(handle)
    except (OSError, ValidationError) as exc:
        raise TailCloseValidationError("oos_registry_invalid") from exc

    precommit = next(
        (
            item
            for item in records
            if item.get("record_type") == "precommit"
            and item.get("precommit_id") == precommit_id
        ),
        None,
    )
    if precommit is None:
        raise TailCloseValidationError("precommit_missing")
    if not verify_oos_precommit_record(precommit):
        raise TailCloseValidationError("precommit_record_invalid")
    precommit_core = {
        key: precommit.get(key)
        for key in (
            "schema_version",
            "ancestor_commit",
            "clean_tree",
            "rules_sha256",
            "dataset_sha256",
            "thresholds_sha256",
            "split",
            "variants",
            "fold_ids",
        )
    }
    if (
        canonical_hash(precommit_core) != precommit_id
        or not precommit.get("invocation_id")
        or not precommit.get("process_instance_id")
    ):
        raise TailCloseValidationError("precommit_identity_invalid")

    reveal = next(
        (
            item
            for item in records
            if item.get("record_type") == "result"
            and item.get("precommit_id") == precommit_id
            and item.get("record_sha256") == reveal_record_sha256
        ),
        None,
    )
    if reveal is None:
        raise TailCloseValidationError("reveal_record_missing")
    if (
        reveal.get("schema_version") != "oos-result-v1"
        or reveal.get("status") != "registered"
        or reveal.get("invocation_id") == precommit.get("invocation_id")
    ):
        raise TailCloseValidationError("precommit_reveal_not_independent")
    reveal_core = {
        key: reveal.get(key)
        for key in (
            "schema_version",
            "precommit_id",
            "revealed_from_commit",
            "variants",
            "folds",
        )
    }
    if reveal.get("artifact_sha256") != canonical_hash(reveal_core):
        raise TailCloseValidationError("reveal_artifact_invalid")

    try:
        dataset_sha256 = _file_sha256(dataset_path)
        dataset_payload = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TailCloseValidationError("oos_dataset_unavailable") from exc
    if dataset_sha256 != precommit.get("dataset_sha256"):
        raise TailCloseValidationError("oos_dataset_hash_mismatch")
    registered_outcomes = (
        dataset_payload.get("outcomes")
        if isinstance(dataset_payload, Mapping)
        else dataset_payload
    )
    if (
        not isinstance(registered_outcomes, list)
        or canonical_hash(registered_outcomes) != canonical_hash(list(outcomes))
    ):
        raise TailCloseValidationError("outcomes_not_bound_to_dataset")
    return dict(precommit), dict(reveal)


def _classify_outcomes(
    outcomes: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    trades = []
    pending = 0
    censored = 0
    invalid = 0
    seen_trade_ids: set[str] = set()
    for outcome in outcomes:
        trade_id = str(outcome.get("trade_id") or outcome.get("signal_id") or "")
        if not trade_id or trade_id in seen_trade_ids:
            invalid += 1
            continue
        seen_trade_ids.add(trade_id)
        status = str(outcome.get("status") or "")
        if outcome.get("right_censored") is True or status == "right_censored":
            censored += 1
            continue
        if outcome.get("observation_complete") is not True or status == "blocked_pending":
            pending += 1
            continue
        return_value = outcome.get("net_return")
        if status != "exited" or return_value is None:
            invalid += 1
            continue
        trades.append(
            {
                "trade_id": trade_id,
                "stock": str(outcome.get("code") or ""),
                "session": str(outcome.get("trading_date") or ""),
                "regime": str(outcome.get("regime") or ""),
                "return": float(return_value),
            }
        )
    counts = {
        "submitted": len(outcomes),
        "eligible": len(trades),
        "pending": pending,
        "right_censored": censored,
        "invalid": invalid,
    }
    return trades, counts


def evaluate_oos_family(
    *,
    strategy_id: str,
    outcomes: Sequence[Mapping[str, Any]],
    variant_returns: Mapping[str, Sequence[float]],
    precommit_registry_path: str | Path,
    precommit_id: str,
    reveal_record_sha256: str,
    dataset_path: str | Path,
    validation_thresholds_path: str | Path,
    strategy_config: Mapping[str, Any],
    validation_thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one strategy family without borrowing evidence from its sibling."""
    if strategy_id not in STRATEGY_FAMILIES:
        raise TailCloseValidationError("strategy_family_unknown")
    precommit, reveal = _load_registered_oos_evidence(
        registry_path=precommit_registry_path,
        precommit_id=precommit_id,
        reveal_record_sha256=reveal_record_sha256,
        dataset_path=dataset_path,
        outcomes=outcomes,
    )
    try:
        frozen_thresholds = json.loads(
            Path(validation_thresholds_path).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TailCloseValidationError("validation_thresholds_unavailable") from exc
    if (
        _file_sha256(validation_thresholds_path)
        != precommit.get("thresholds_sha256")
    ):
        raise TailCloseValidationError("validation_thresholds_hash_mismatch")
    if (
        not isinstance(frozen_thresholds, Mapping)
        or canonical_hash(
            {
                key: value
                for key, value in frozen_thresholds.items()
                if key != "config_sha256"
            }
        )
        != canonical_hash(
            {
                key: value
                for key, value in validation_thresholds.items()
                if key != "config_sha256"
            }
        )
    ):
        raise TailCloseValidationError("validation_thresholds_payload_mismatch")
    registration = precommit.get("split") or {}
    if str(registration.get("strategy_id") or "") != strategy_id:
        raise TailCloseValidationError("precommit_strategy_mismatch")
    family_config_hash = strategy_family_config_hash(strategy_config, strategy_id)
    if registration.get("config_hash") != family_config_hash:
        raise TailCloseValidationError("precommit_config_mismatch")
    primary_variant = str(registration.get("primary_variant") or "")
    registered_variants = list(precommit.get("variants") or [])
    if (
        not primary_variant
        or primary_variant not in registered_variants
        or set(variant_returns) != set(registered_variants)
    ):
        raise TailCloseValidationError("precommit_variants_mismatch")
    trades, outcome_counts = _classify_outcomes(outcomes)
    raw_returns = list(float(value) for value in variant_returns[primary_variant])
    if len(raw_returns) != len(trades):
        raise TailCloseValidationError("primary_returns_outcome_mismatch")
    outcome_returns = [float(item["return"]) for item in trades]
    if raw_returns != outcome_returns:
        raise TailCloseValidationError("primary_returns_not_derived_from_outcomes")
    if any(
        str(item.get("strategy_id") or "") != strategy_id
        for item in outcomes
    ):
        raise TailCloseValidationError("cross_family_outcome")
    revealed_variants = {
        str(item.get("variant_id") or ""): item
        for item in (reveal.get("variants") or [])
        if isinstance(item, Mapping)
    }
    if set(revealed_variants) != set(registered_variants):
        raise TailCloseValidationError("reveal_variants_mismatch")
    for variant, values in variant_returns.items():
        reveal_variant = revealed_variants[variant]
        normalised_values = list(map(float, values))
        sample_count = reveal_variant.get("sample_count")
        if (
            len(normalised_values) != len(trades)
            or reveal_variant.get("returns_hash") != canonical_hash(normalised_values)
            or isinstance(sample_count, bool)
            or not isinstance(sample_count, int)
            or sample_count != len(normalised_values)
        ):
            raise TailCloseValidationError("returns_not_bound_to_reveal")

    p_values = {
        variant: _normal_mean_p_value(list(map(float, values)))
        for variant, values in variant_returns.items()
    }
    statistics_config = validation_thresholds["statistics"]
    if len(raw_returns) < int(statistics_config["minimum_observations"]):
        statistics_core = {
            "schema_version": "statistical-validation-suite-v1",
            "computed_by": "tail-close-validation-v1",
            "primary_variant": primary_variant,
            "calculations": {},
            "decision_thresholds_sha256": canonical_hash(dict(statistics_config)),
            "status": "not_evaluated",
            "reasons": ["completed_sample_insufficient"],
        }
        statistics_result = {
            **statistics_core,
            "artifact_sha256": canonical_hash(statistics_core),
        }
    else:
        statistics_result = compute_statistical_validation(
            primary_variant=primary_variant,
            variant_returns=variant_returns,
            p_values=p_values,
            config=statistics_config,
            seed=int(registration.get("seed") or 0),
        )
    effective = compute_effective_samples(trades)
    profit_factor = _profit_factor(raw_returns)
    oos_config = strategy_config["validation"]["oos"]
    raw_minimum = int(oos_config["minimum_simulated_filled_samples"])
    empirical = validation_thresholds["empirical"]
    cost_stress_bps = float(
        oos_config["maximum_impact_stress_bps_with_nonnegative_expectancy"]
    )
    stressed_expectancy = (
        statistics.fmean(raw_returns) - cost_stress_bps / 10_000
        if raw_returns
        else None
    )
    blocked_count = sum(1 for item in outcomes if int(item.get("days_blocked") or 0) > 0)
    censored_count = outcome_counts["right_censored"]
    eligible_outcomes = [
        item
        for item in outcomes
        if str(item.get("status") or "") == "exited"
        and item.get("observation_complete") is True
        and item.get("right_censored") is not True
    ]
    incremental = [
        float(item["incremental_net_return"])
        for item in eligible_outcomes
        if item.get("incremental_net_return") is not None
    ]
    outcome_dates = []
    for item in eligible_outcomes:
        try:
            outcome_dates.append(date.fromisoformat(str(item.get("trading_date") or "")))
        except ValueError as exc:
            raise TailCloseValidationError("outcome_trading_date_invalid") from exc
    history_years = (
        (max(outcome_dates) - min(outcome_dates)).days / 365.25
        if len(outcome_dates) >= 2
        else 0.0
    )
    protocol_frozen = all(
        (
            registration.get("method") == "walk_forward",
            registration.get("purge_and_embargo") is True,
            registration.get("multiple_testing_correction") is True,
        )
    )
    observed_broker_calls = sum(
        int(item.get("broker_call_count") or 0) for item in outcomes
    )
    observed_automatic_orders = sum(
        int(item.get("automatic_order_count") or 0) for item in outcomes
    )
    maximum_live_weight = max(
        (float(item.get("live_weight") or 0) for item in outcomes),
        default=0.0,
    )
    checks = {
        "history_coverage_sufficient": history_years
        >= float(oos_config["minimum_history_years"]),
        "precommit_protocol_frozen": protocol_frozen,
        "outcome_accounting_conserved": (
            outcome_counts["submitted"]
            == outcome_counts["eligible"]
            + outcome_counts["pending"]
            + outcome_counts["right_censored"]
            + outcome_counts["invalid"]
        ),
        "all_outcomes_complete": (
            outcome_counts["pending"] == 0
            and outcome_counts["right_censored"] == 0
            and outcome_counts["invalid"] == 0
        ),
        "research_execution_isolated": (
            all(item.get("research_only") is True for item in outcomes)
            and observed_broker_calls == 0
            and observed_automatic_orders == 0
            and maximum_live_weight == 0
        ),
        "raw_sample_sufficient": len(trades) >= raw_minimum,
        "effective_trade_sufficient": (
            effective.get("status") == "evaluated"
            and float(effective.get("trade") or 0)
            >= float(empirical["minimum_trade_effective_samples"])
        ),
        "effective_stock_sufficient": (
            effective.get("status") == "evaluated"
            and float(effective.get("stock") or 0)
            >= float(empirical["minimum_stock_effective_samples"])
        ),
        "effective_regime_sufficient": (
            effective.get("status") == "evaluated"
            and float(effective.get("regime") or 0)
            >= float(empirical["minimum_regime_effective_samples"])
        ),
        "statistics_passed": statistics_result.get("status") == "passed",
        "profit_factor_passed": (
            profit_factor is not None
            and profit_factor >= float(oos_config["minimum_profit_factor"])
        ),
        "cost_stress_passed": (
            stressed_expectancy is not None and stressed_expectancy > 0
        ),
        "incremental_passed": (
            len(incremental) == len(trades)
            and bool(incremental)
            and statistics.fmean(incremental) > 0
        ),
        "censoring_within_limit": (
            censored_count / outcome_counts["submitted"]
            if outcome_counts["submitted"]
            else 1.0
        )
        <= float(oos_config["maximum_censored_ratio"]),
    }
    reasons = sorted(name for name, passed in checks.items() if not passed)
    result = {
        "schema": SCHEMA,
        "strategy_id": strategy_id,
        "strategy_family": STRATEGY_FAMILIES[strategy_id],
        "precommit_id": precommit_id,
        "precommit_record_sha256": precommit["record_sha256"],
        "precommit_invocation_id": precommit["invocation_id"],
        "reveal_record_sha256": reveal["record_sha256"],
        "reveal_invocation_id": reveal["invocation_id"],
        "dataset_sha256": precommit["dataset_sha256"],
        "config_hash": family_config_hash,
        "status": "passed" if not reasons else "failed",
        "allowed_next_state": "shadow" if not reasons else "research_only",
        "checks": checks,
        "reasons": reasons,
        "raw_filled_trades": len(trades),
        "submitted_outcomes": outcome_counts["submitted"],
        "pending_outcomes": outcome_counts["pending"],
        "invalid_outcomes": outcome_counts["invalid"],
        "effective_samples": effective,
        "statistics": statistics_result,
        "profit_factor": profit_factor,
        "cost_stress_bps": cost_stress_bps,
        "stressed_expectancy": stressed_expectancy,
        "blocked_count": blocked_count,
        "right_censored_count": censored_count,
        "incremental_observations": len(incremental),
        "history_years": round(history_years, 4),
        "live_weight": maximum_live_weight,
        "broker_call_count": observed_broker_calls,
        "automatic_order_count": observed_automatic_orders,
    }
    result["artifact_hash"] = canonical_hash(result)
    return result


def evaluate_shadow_readiness(
    *,
    strategy_id: str,
    observations: Sequence[Mapping[str, Any]],
    oos_result: Mapping[str, Any],
    strategy_config: Mapping[str, Any],
    validation_thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    if strategy_id not in STRATEGY_FAMILIES:
        raise TailCloseValidationError("strategy_family_unknown")
    unique_days = set()
    for item in observations:
        trading_date = str(item.get("trading_date") or "")
        try:
            valid_trading_date = is_trading_day(trading_date)
        except (CalendarCoverageError, TypeError, ValueError):
            valid_trading_date = False
        if (
            valid_trading_date
            and item.get("observation_mode") == "live_shadow"
            and item.get("signal_frozen_before_outcome") is True
            and item.get("live_effect") == "none"
            and item.get("point_in_time_valid") is True
        ):
            unique_days.add(trading_date)
    minimum_days = max(
        int(validation_thresholds["shadow"]["minimum_trading_days"]),
        int(strategy_config["validation"]["shadow"]["minimum_real_trading_days"]),
    )
    major_incidents = sum(
        1 for item in observations if item.get("major_incident") is True
    )
    maximum_error = max(
        [
            float(item.get("simulation_error") or 0)
            for item in observations
        ],
        default=0.0,
    )
    observed_broker_calls = sum(
        int(item.get("broker_call_count") or 0) for item in observations
    )
    observed_automatic_orders = sum(
        int(item.get("automatic_order_count") or 0) for item in observations
    )
    maximum_live_weight = max(
        (float(item.get("live_weight") or 0) for item in observations),
        default=0.0,
    )
    checks = {
        "matching_oos_passed": (
            _artifact_valid(oos_result, SCHEMA)
            and oos_result.get("strategy_id") == strategy_id
            and oos_result.get("status") == "passed"
            and oos_result.get("allowed_next_state") == "shadow"
            and oos_result.get("config_hash")
            == strategy_family_config_hash(strategy_config, strategy_id)
        ),
        "observation_family_isolated": all(
            item.get("strategy_id") == strategy_id for item in observations
        ),
        "trading_days_sufficient": len(unique_days) >= minimum_days,
        "no_major_incidents": major_incidents == 0,
        "simulation_error_within_limit": maximum_error
        <= float(validation_thresholds["shadow"]["maximum_simulation_error"]),
        "shadow_contract_complete": all(
            all(
                field in item
                for field in (
                    "simulation_error",
                    "broker_call_count",
                    "automatic_order_count",
                    "live_weight",
                    "ledger_audit_mismatches",
                    "pit_sla_incidents",
                )
            )
            for item in observations
        ),
        "ledger_and_pit_incidents_zero": all(
            int(item.get("ledger_audit_mismatches") or 0) == 0
            and int(item.get("pit_sla_incidents") or 0) == 0
            for item in observations
        ),
        "broker_never_called": all(
            int(item.get("broker_call_count") or 0) == 0
            for item in observations
        ),
        "live_weight_zero": all(
            float(item.get("live_weight") or 0) == 0
            for item in observations
        ),
        "automatic_orders_zero": all(
            int(item.get("automatic_order_count") or 0) == 0
            for item in observations
        ),
    }
    reasons = sorted(name for name, passed in checks.items() if not passed)
    result = {
        "schema": "tail_close_shadow_readiness_v1",
        "strategy_id": strategy_id,
        "config_hash": strategy_family_config_hash(strategy_config, strategy_id),
        "oos_artifact_hash": oos_result.get("artifact_hash"),
        "status": "passed" if not reasons else "not_ready",
        "allowed_next_state": (
            "eligible_for_manual_pilot" if not reasons else "research_only"
        ),
        "checks": checks,
        "reasons": reasons,
        "trading_days": len(unique_days),
        "minimum_trading_days": minimum_days,
        "major_incidents": major_incidents,
        "maximum_simulation_error": maximum_error,
        "live_weight": maximum_live_weight,
        "broker_call_count": observed_broker_calls,
        "automatic_order_count": observed_automatic_orders,
    }
    result["artifact_hash"] = canonical_hash(result)
    return result


def evaluate_manual_pilot_eligibility(
    *,
    strategy_id: str,
    oos_result: Mapping[str, Any],
    shadow_result: Mapping[str, Any],
    explicit_human_approval: bool,
) -> dict[str, Any]:
    if strategy_id not in STRATEGY_FAMILIES:
        raise TailCloseValidationError("strategy_family_unknown")
    checks = {
        "matching_oos_family": (
            _artifact_valid(oos_result, SCHEMA)
            and oos_result.get("strategy_id") == strategy_id
        ),
        "matching_shadow_family": (
            _artifact_valid(
                shadow_result,
                "tail_close_shadow_readiness_v1",
            )
            and shadow_result.get("strategy_id") == strategy_id
            and shadow_result.get("oos_artifact_hash")
            == oos_result.get("artifact_hash")
            and shadow_result.get("config_hash") == oos_result.get("config_hash")
        ),
        "oos_passed": (
            oos_result.get("status") == "passed"
            and oos_result.get("allowed_next_state") == "shadow"
        ),
        "shadow_passed": (
            shadow_result.get("status") == "passed"
            and shadow_result.get("allowed_next_state")
            == "eligible_for_manual_pilot"
        ),
        "explicit_human_approval": explicit_human_approval is True,
    }
    reasons = sorted(name for name, passed in checks.items() if not passed)
    result = {
        "schema": "tail_close_manual_pilot_gate_v1",
        "strategy_id": strategy_id,
        "status": "eligible_for_manual_pilot" if not reasons else "not_eligible",
        "checks": checks,
        "reasons": reasons,
        "system_ordering": "forbidden",
        "human_decision_and_order_required": True,
        "oos_artifact_hash": oos_result.get("artifact_hash"),
        "shadow_artifact_hash": shadow_result.get("artifact_hash"),
        "live_weight": 0.0,
        "broker_call_count": 0,
        "automatic_order_count": 0,
    }
    result["artifact_hash"] = canonical_hash(result)
    return result
