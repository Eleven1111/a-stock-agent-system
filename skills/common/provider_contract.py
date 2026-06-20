"""Small provider-result contract used by fallback product adapters."""

from __future__ import annotations

from typing import Any


def observation_ok(provider: str, data: Any) -> dict[str, Any]:
    return {
        "status": "ok",
        "provider": provider,
        "data": data,
        "error": None,
    }


def observation_error(provider: str, error: Exception | dict | str) -> dict[str, Any]:
    if isinstance(error, dict):
        detail = dict(error)
    elif hasattr(error, "to_dict"):
        detail = error.to_dict()
    else:
        detail = {
            "type": type(error).__name__,
            "message": str(error),
        }
    return {
        "status": "error",
        "provider": provider,
        "data": None,
        "error": detail,
    }


def health_attempt(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": observation.get("provider"),
        "status": observation.get("status"),
        "error": observation.get("error"),
    }
