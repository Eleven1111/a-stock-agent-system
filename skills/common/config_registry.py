"""Central config paths and schema roots with domain-local loading."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import yaml


CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
SPECS: dict[str, dict[str, Any]] = {
    "calendar": {
        "filename": "a_share_calendar.json",
        "format": "json",
        "required_roots": {"schema", "source", "covered_years", "closed_dates"},
    },
    "candidate_selection": {
        "filename": "candidate_selection.json",
        "format": "json",
        "required_roots": {"version", "network", "pipeline", "universe"},
    },
    "daban_thresholds": {
        "filename": "daban_thresholds.yaml",
        "format": "yaml",
        "required_roots": {"cost", "auction", "universe", "market_gate"},
    },
    "data_access": {
        "filename": "data_access.json",
        "format": "json",
        "required_roots": {"providers", "risk", "storage"},
    },
    "nl_screening": {
        "filename": "nl_screening.yaml",
        "format": "yaml",
        "required_roots": {"version", "eastmoney", "wencai", "queries"},
    },
    "reflexivity_strategy": {
        "filename": "reflexivity_strategy.json",
        "format": "json",
        "required_roots": {"schema", "version", "thresholds"},
    },
    "paper_trading": {
        "filename": "paper_trading.json",
        "format": "json",
        "required_roots": {"schema", "version", "account", "entry_gate", "execution"},
    },
    "scoring": {
        "filename": "scoring.yaml",
        "format": "yaml",
        "required_roots": {"scoring", "risk"},
    },
    "tail_close_strategy": {
        "filename": "tail_close_strategy.json",
        "format": "json",
        "required_roots": {
            "schema",
            "version",
            "strategies",
            "runtime",
            "universe",
            "market_gate",
            "sector_gate",
            "stock_gate",
            "ranking",
            "execution",
            "exit",
            "portfolio",
            "validation",
            "safety",
        },
    },
}


class ConfigError(RuntimeError):
    pass


_TAIL_CLOSE_PRIMARY_ID = "tail_close:mainline_continuation_v1"
_TAIL_CLOSE_SIBLING_ID = "tail_close:after_hours_fixed_v1"
_TAIL_CLOSE_STRATEGY_IDS = {
    _TAIL_CLOSE_PRIMARY_ID,
    _TAIL_CLOSE_SIBLING_ID,
}
_TAIL_CLOSE_RUNTIME_VALUES = {
    "prepare_cutoff": "14:34:59",
    "prepare_maximum_current_record_age_seconds": 60,
    "maximum_clock_offset_seconds": 2,
    "decision_cutoff": "14:49:59",
    "decision_maximum_current_record_age_seconds": 10,
    "deadline": "14:50:20",
    "cancel": "14:56:30",
}


def _mapping_field(
    payload: Mapping[str, Any],
    field: str,
    *,
    context: str,
) -> Mapping[str, Any]:
    value = payload.get(field)
    if not isinstance(value, Mapping):
        raise ConfigError(f"{context}.{field} must be an object")
    return value


def _require_fields(
    payload: Mapping[str, Any],
    fields: set[str],
    *,
    context: str,
) -> None:
    missing = sorted(fields - set(payload))
    if missing:
        raise ConfigError(f"{context} missing required fields: {missing}")


def _validate_tail_close_strategy(payload: Mapping[str, Any]) -> None:
    strategies = _mapping_field(payload, "strategies", context="tail_close_strategy")
    if set(strategies) != _TAIL_CLOSE_STRATEGY_IDS:
        raise ConfigError(
            "tail_close_strategy.strategies must contain only the fixed strategy IDs"
        )
    required_strategy_fields = {
        "strategy_id",
        "strategy_lane",
        "decision_time",
        "pit_cutoff",
        "entry_session",
        "entry_window",
        "promotion_state",
        "live_weight",
        "broker_access",
        "automatic_ordering",
    }
    for strategy_id in sorted(_TAIL_CLOSE_STRATEGY_IDS):
        strategy = _mapping_field(strategies, strategy_id, context="strategies")
        _require_fields(
            strategy,
            required_strategy_fields,
            context=f"strategies.{strategy_id}",
        )
        if strategy.get("strategy_id") != strategy_id:
            raise ConfigError(f"strategies.{strategy_id}.strategy_id mismatch")
        if strategy.get("strategy_lane") != "tail_close":
            raise ConfigError(f"strategies.{strategy_id}.strategy_lane must be tail_close")
        if (
            strategy.get("promotion_state") != "research_only"
            or strategy.get("live_weight") != 0
            or strategy.get("broker_access") != "forbidden"
            or strategy.get("automatic_ordering") != "forbidden"
        ):
            raise ConfigError(f"strategies.{strategy_id} violates research-only safety")

    primary = strategies[_TAIL_CLOSE_PRIMARY_ID]
    if (
        primary.get("decision_time") != "14:50:00"
        or primary.get("pit_cutoff") != "14:49:59"
        or primary.get("entry_session") != "continuous_auction"
    ):
        raise ConfigError(
            f"strategies.{_TAIL_CLOSE_PRIMARY_ID} must use the fixed "
            "14:50 continuous-auction session"
        )
    sibling = strategies[_TAIL_CLOSE_SIBLING_ID]
    if (
        sibling.get("decision_time") != "15:05:00"
        or sibling.get("pit_cutoff") != "15:04:59"
        or sibling.get("entry_session") != "after_hours_fixed_price"
    ):
        raise ConfigError(
            f"strategies.{_TAIL_CLOSE_SIBLING_ID} must use the fixed "
            "15:05 after-hours session"
        )

    runtime = _mapping_field(payload, "runtime", context="tail_close_strategy")
    _require_fields(
        runtime,
        set(_TAIL_CLOSE_RUNTIME_VALUES) | {"plugin_contract"},
        context="tail_close_strategy.runtime",
    )
    for field, expected in _TAIL_CLOSE_RUNTIME_VALUES.items():
        if runtime.get(field) != expected:
            raise ConfigError(
                f"tail_close_strategy.runtime.{field} must be {expected}"
            )
    plugin_contract = _mapping_field(runtime, "plugin_contract", context="runtime")
    _require_fields(
        plugin_contract,
        {"protocol", "methods", "side_effects", "shared_runtime_owners"},
        context="tail_close_strategy.runtime.plugin_contract",
    )
    if plugin_contract.get("methods") != [
        "prepare",
        "gate",
        "rank",
        "simulate_execution",
        "label_outcome",
    ]:
        raise ConfigError("tail_close_strategy plugin method contract mismatch")
    if plugin_contract.get("side_effects") != "none":
        raise ConfigError("tail_close_strategy plugin must be side-effect free")

    stock_gate = _mapping_field(payload, "stock_gate", context="tail_close_strategy")
    _require_fields(
        stock_gate,
        {
            "minimum_tail_minutes",
            "maximum_single_minute_gain_share",
            "minimum_last10_return",
            "maximum_single_minute_loss",
            "volume_selloff_multiple",
        },
        context="tail_close_strategy.stock_gate",
    )
    sector_gate = _mapping_field(
        payload,
        "sector_gate",
        context="tail_close_strategy",
    )
    _require_fields(
        sector_gate,
        {"minimum_breadth"},
        context="tail_close_strategy.sector_gate",
    )
    execution = _mapping_field(payload, "execution", context="tail_close_strategy")
    _require_fields(
        execution,
        {
            "observed_system_latency_seconds",
            "manual_review_latency_seconds",
            "research_notional_per_signal_cny",
            "after_hours_queue_discount",
            "maximum_limit_premium",
        },
        context="tail_close_strategy.execution",
    )
    validation = _mapping_field(payload, "validation", context="tail_close_strategy")
    oos = _mapping_field(validation, "oos", context="validation")
    _require_fields(
        oos,
        {"maximum_censored_ratio"},
        context="tail_close_strategy.validation.oos",
    )
    shadow = _mapping_field(validation, "shadow", context="validation")
    _require_fields(
        shadow,
        {"maximum_fill_rate_error"},
        context="tail_close_strategy.validation.shadow",
    )
    portfolio = _mapping_field(payload, "portfolio", context="tail_close_strategy")
    _require_fields(
        portfolio,
        {
            "research_portfolio_notional_cny",
            "maximum_single_position_pct",
            "maximum_sector_exposure_pct",
        },
        context="tail_close_strategy.portfolio",
    )

    safety = _mapping_field(payload, "safety", context="tail_close_strategy")
    _require_fields(
        safety,
        {
            "research_only",
            "live_weight",
            "broker_access",
            "broker_call_count",
            "automatic_ordering",
            "automatic_order_count",
        },
        context="tail_close_strategy.safety",
    )
    if (
        safety.get("research_only") is not True
        or safety.get("live_weight") != 0
        or safety.get("broker_access") != "forbidden"
        or safety.get("broker_call_count") != 0
        or safety.get("automatic_ordering") != "forbidden"
        or safety.get("automatic_order_count") != 0
    ):
        raise ConfigError("tail_close_strategy safety must enforce zero live execution")


def config_sha256(payload: Mapping[str, Any]) -> str:
    """Return a stable digest for a validated config payload."""
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"config cannot be canonically hashed: {exc}") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def config_path(name: str) -> Path:
    try:
        filename = SPECS[name]["filename"]
    except KeyError as exc:
        raise ConfigError(f"unknown config: {name}") from exc
    return CONFIG_DIR / filename


def load_registered(
    name: str,
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    if name not in SPECS:
        raise ConfigError(f"unknown config: {name}")
    spec = SPECS[name]
    source = Path(path) if path is not None else config_path(name)
    try:
        with source.open(encoding="utf-8") as handle:
            payload = (
                json.load(handle)
                if spec["format"] == "json"
                else yaml.safe_load(handle)
            )
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot load {name} config from {source}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ConfigError(f"{name} config root must be an object")
    missing = sorted(set(spec["required_roots"]) - set(payload))
    if missing:
        raise ConfigError(f"{name} config missing required root fields: {missing}")
    if name == "tail_close_strategy":
        _validate_tail_close_strategy(payload)
    return dict(payload)


def validate_registered_configs() -> dict[str, Any]:
    configs: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    for name in SPECS:
        try:
            payload = load_registered(name)
            configs[name] = {
                "path": str(config_path(name)),
                "status": "ok",
                "roots": sorted(payload),
                "sha256": config_sha256(payload),
            }
        except ConfigError as exc:
            configs[name] = {
                "path": str(config_path(name)),
                "status": "error",
                "error": str(exc),
            }
            errors.append({"config": name, "error": str(exc)})
    return {
        "schema": "a_stock_config_report_v1",
        "status": "ok" if not errors else "error",
        "configs": configs,
        "errors": errors,
    }
