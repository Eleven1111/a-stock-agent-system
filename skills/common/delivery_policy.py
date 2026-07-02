"""Shared delivery policy switches for cron output reduction."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping


DEFAULT_POLICY: dict[str, Any] = {
    "schema": "delivery_policy_v1",
    "novelty_gate": {
        "enabled": True,
        "mode": "enforce",
        "ttl_days": 7,
    },
    "summary_mode": {
        "enabled": True,
        "mode": "enforce",
    },
    "adaptive_backoff": {
        "enabled": True,
        "mode": "shadow",
    },
}


def default_policy() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_POLICY)


def policy_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "delivery_policy.json"


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    policy = default_policy()
    target = Path(path) if path else policy_path()
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return policy
    if not isinstance(loaded, dict):
        return policy
    for section, defaults in DEFAULT_POLICY.items():
        if section == "schema":
            continue
        value = loaded.get(section)
        if isinstance(defaults, dict) and isinstance(value, dict):
            merged = dict(defaults)
            merged.update(value)
            policy[section] = merged
    if isinstance(loaded.get("schema"), str):
        policy["schema"] = loaded["schema"]
    return policy


def section(policy: Mapping[str, Any] | None, name: str) -> dict[str, Any]:
    base = default_policy().get(name, {})
    configured = (policy or {}).get(name) if isinstance(policy, Mapping) else None
    if isinstance(base, dict) and isinstance(configured, Mapping):
        merged = dict(base)
        merged.update(configured)
        return merged
    return dict(base) if isinstance(base, dict) else {}


def enabled(policy: Mapping[str, Any] | None, name: str) -> bool:
    return bool(section(policy, name).get("enabled", True))


def mode(policy: Mapping[str, Any] | None, name: str) -> str:
    value = str(section(policy, name).get("mode") or "enforce").lower()
    return value if value in {"enforce", "shadow"} else "enforce"


def shadow(policy: Mapping[str, Any] | None, name: str) -> bool:
    return enabled(policy, name) and mode(policy, name) == "shadow"


def enforce(policy: Mapping[str, Any] | None, name: str) -> bool:
    return enabled(policy, name) and mode(policy, name) == "enforce"
