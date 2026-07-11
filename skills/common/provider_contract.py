"""Small provider-result contract used by fallback product adapters."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


def transport_contract(url: str) -> dict[str, Any]:
    """Describe transport trust; plaintext observations cannot solely drive direction."""
    scheme = urlparse(url).scheme.lower()
    if scheme == "https":
        return {
            "scheme": scheme,
            "trust": "authenticated",
            "directional_eligible": True,
            "reason": "authenticated_transport",
        }
    return {
        "scheme": scheme or "unknown",
        "trust": "lower",
        "directional_eligible": False,
        "reason": "transport_lower_trust",
    }


def prevent_https_downgrade(requested_url: str, resolved_url: str) -> None:
    """Reject redirect/resolution from authenticated HTTPS to plaintext HTTP."""
    requested = urlparse(requested_url).scheme.lower()
    resolved = urlparse(resolved_url).scheme.lower()
    if requested == "https" and resolved != "https":
        raise ValueError("https_downgrade")


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
