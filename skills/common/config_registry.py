"""Central config paths and schema roots with domain-local loading."""

from __future__ import annotations

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
    "scoring": {
        "filename": "scoring.yaml",
        "format": "yaml",
        "required_roots": {"scoring", "risk"},
    },
}


class ConfigError(RuntimeError):
    pass


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
