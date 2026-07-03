"""Field-level multi-source arbitration.

Wraps an ordered list of ``(provider, fetcher)`` candidates for a single
logical field (e.g. capital flow) and resolves to the first source that is
both healthy (circuit not open) and successfully returns data. Every attempt
is recorded to ``provider_health`` so the SLO ledger and breaker stay in sync
with real traffic, and callers stop paying the full retry cost of a source
that is already known to be down.

Fail-closed: if every candidate is unhealthy or fails, ``resolve`` returns a
``provider_contract.observation_error`` payload — it never fabricates data
from a partially failed chain.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

try:
    from . import provider_health
    from .data_access_config import field_chains_settings
    from .provider_contract import observation_error, observation_ok
except ImportError:  # pragma: no cover - script-style sys.path imports
    import provider_health
    from data_access_config import field_chains_settings
    from provider_contract import observation_error, observation_ok


__all__ = ["resolve", "compare_sources", "field_chain"]

Fetcher = Callable[[], Any]


def field_chain(data_type: str) -> list[str]:
    """Configured provider priority order for a data type, or an empty list."""
    return list(field_chains_settings().get(data_type, []))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _try_fetch(
    provider: str,
    fetch: Fetcher,
    endpoint_class: str,
    moment: datetime,
    probe_token: str | None,
) -> tuple[bool, Any]:
    """Call ``fetch`` once, recording the outcome to provider_health.

    The fetch runs inside ``suppress_transport_recording`` so http_client's
    transport-level bookkeeping is skipped: one physical request is recorded
    exactly once, in this arbiter's (provider, endpoint_class) bucket.
    """
    started = time.monotonic()
    try:
        with provider_health.suppress_transport_recording():
            data = fetch()
    except Exception as exc:  # noqa: BLE001 - any fetcher failure is a source failure
        latency_ms = (time.monotonic() - started) * 1000
        provider_health.record_result(
            provider, endpoint_class, False, latency_ms, now=moment, probe_token=probe_token,
        )
        return False, {"provider": provider, "error": str(exc), "type": type(exc).__name__}
    latency_ms = (time.monotonic() - started) * 1000
    provider_health.record_result(
        provider, endpoint_class, True, latency_ms, now=moment, probe_token=probe_token,
    )
    return True, data


def resolve(
    data_type: str,
    fetchers: Iterable[tuple[str, Fetcher]],
    *,
    endpoint_class: str = "default",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Call the first healthy, successful fetcher in priority order.

    ``fetchers`` is an ordered ``[(provider, callable)]`` list; order is the
    priority. Providers whose circuit is open are skipped without being
    called. Every attempted call's outcome is recorded to provider_health.
    """
    moment = now or _utc_now()
    skipped: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for provider, fetch in fetchers:
        gate = provider_health.allow_request(provider, endpoint_class, now=moment)
        if not gate["allowed"]:
            skipped.append({"provider": provider, "reason": gate["reason"], "state": gate["state"]})
            continue

        ok, payload = _try_fetch(provider, fetch, endpoint_class, moment, gate.get("probe_token"))
        if not ok:
            failures.append(payload)
            continue

        result = observation_ok(provider, payload)
        result["data_type"] = data_type
        if skipped or failures:
            result["degraded"] = {"skipped": skipped, "failures": failures}
        return result

    detail = {
        "message": f"all sources exhausted for {data_type}",
        "skipped": skipped,
        "failures": failures,
    }
    result = observation_error(f"{data_type}_chain", detail)
    result["data_type"] = data_type
    result["degraded"] = {"skipped": skipped, "failures": failures}
    return result


def compare_sources(
    field_name: str,
    values_by_provider: dict[str, float],
    *,
    tolerance_pct: float,
) -> dict[str, Any]:
    """Cross-source consistency spot check for a numeric field.

    Compares every pair of provider values; if any pair's relative deviation
    exceeds ``tolerance_pct`` (percent of the larger magnitude), the field is
    flagged ``cross_source_mismatch`` so downstream snapshots can carry a
    quality flag without silently trusting either source.
    """
    numeric = {
        provider: float(value)
        for provider, value in values_by_provider.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    if len(numeric) < 2:
        return {
            "field": field_name,
            "consistent": True,
            "quality_flag": None,
            "values": numeric,
        }

    providers = sorted(numeric)
    mismatches: list[dict[str, Any]] = []
    for i, left in enumerate(providers):
        for right in providers[i + 1:]:
            left_value, right_value = numeric[left], numeric[right]
            scale = max(abs(left_value), abs(right_value), 1e-9)
            deviation_pct = abs(left_value - right_value) / scale * 100
            if deviation_pct > tolerance_pct:
                mismatches.append({
                    "providers": [left, right],
                    "values": [left_value, right_value],
                    "deviation_pct": round(deviation_pct, 4),
                })

    consistent = not mismatches
    return {
        "field": field_name,
        "consistent": consistent,
        "quality_flag": None if consistent else "cross_source_mismatch",
        "values": numeric,
        "mismatches": mismatches,
    }
