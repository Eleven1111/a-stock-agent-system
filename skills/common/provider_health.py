"""Data-source SLO ledger and circuit breaker.

Every provider call result is appended to a rolling per-(provider,
endpoint_class) window persisted under
``$A_STOCK_STATE_HOME/runtime/provider_health/{provider}.json``. The circuit
breaker derived from that window follows the standard
closed -> open -> half_open -> closed state machine so that once a source is
known to be unhealthy, callers stop paying full timeout+retry cost on every
request instead of discovering the outage independently each time.

``allow_request`` is the single place that transitions open -> half_open and
claims the one-shot probe slot; it does so inside a single ``mutate_json``
call so concurrent callers racing the same cooldown window cannot both slip
through as the probe (thundering herd on recovery). The claim carries a
``probe_claimed_at`` timestamp and a ``probe_token``: if the prober crashes
without reporting, the claim expires after ``probe_ttl_seconds`` and a new
probe slot is issued, and only a result carrying the matching token may
resolve the half-open trial — stale in-flight results from before the
circuit opened cannot close or reopen it.

Fail-closed: this module never fabricates a successful observation. A source
that cannot be probed is reported as blocked, never as healthy.
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

try:
    from .data_access_config import provider_health_settings
    from .paths import hermes_home
    from .state_store import mutate_json, read_json
except ImportError:  # pragma: no cover - script-style sys.path imports
    from data_access_config import provider_health_settings
    from paths import hermes_home
    from state_store import mutate_json, read_json


__all__ = [
    "STATE_CLOSED",
    "STATE_OPEN",
    "STATE_HALF_OPEN",
    "ledger_path",
    "record_result",
    "health_score",
    "allow_request",
    "summary",
    "suppress_transport_recording",
    "transport_recording_suppressed",
]

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"

_TRANSPORT_RECORDING_SUPPRESSED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "provider_health_suppress_transport", default=False
)


@contextmanager
def suppress_transport_recording() -> Iterator[None]:
    """Suppress transport-level (http_client) health recording in this context.

    field_arbiter enters this context around each fetcher call so a single
    physical request is recorded exactly once — in the arbiter's
    (provider, data_type) bucket — instead of also landing in the transport
    "default" bucket and diluting both windows.
    """
    token = _TRANSPORT_RECORDING_SUPPRESSED.set(True)
    try:
        yield
    finally:
        _TRANSPORT_RECORDING_SUPPRESSED.reset(token)


def transport_recording_suppressed() -> bool:
    return bool(_TRANSPORT_RECORDING_SUPPRESSED.get())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso(now: datetime | None = None) -> str:
    return (now or _utc_now()).isoformat(timespec="seconds")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def ledger_path(provider: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in provider)
    return f"{hermes_home()}/runtime/provider_health/{safe}.json"


def _endpoint_key(endpoint_class: str | None) -> str:
    return endpoint_class or "default"


def _empty_circuit() -> dict[str, Any]:
    return {
        "state": STATE_CLOSED,
        "opened_at": None,
        "probe_claimed": False,
        "probe_claimed_at": None,
        "probe_token": None,
    }


def _opened_circuit(moment: datetime) -> dict[str, Any]:
    return {
        "state": STATE_OPEN,
        "opened_at": _now_iso(moment),
        "probe_claimed": False,
        "probe_claimed_at": None,
        "probe_token": None,
    }


def _claimed_probe_circuit(circuit: dict[str, Any], moment: datetime) -> dict[str, Any]:
    return {
        "state": STATE_HALF_OPEN,
        "opened_at": circuit.get("opened_at"),
        "probe_claimed": True,
        "probe_claimed_at": _now_iso(moment),
        "probe_token": uuid.uuid4().hex,
    }


def _load_endpoint(document: Any, endpoint_class: str) -> dict[str, Any]:
    endpoints = document.get("endpoints") if isinstance(document, dict) else None
    endpoint = endpoints.get(endpoint_class) if isinstance(endpoints, dict) else None
    if not isinstance(endpoint, dict):
        return {"window": [], "circuit": _empty_circuit()}
    window = endpoint.get("window")
    circuit = endpoint.get("circuit")
    return {
        "window": window if isinstance(window, list) else [],
        "circuit": circuit if isinstance(circuit, dict) else _empty_circuit(),
    }


def _score_window(window: list[dict[str, Any]]) -> dict[str, Any]:
    samples = len(window)
    successes = sum(1 for item in window if isinstance(item, dict) and item.get("ok"))
    success_rate = successes / samples if samples else None
    return {"samples": samples, "successes": successes, "success_rate": success_rate}


def _resolve_after_result(
    circuit: dict[str, Any],
    window: list[dict[str, Any]],
    entry: dict[str, Any],
    config: dict[str, Any],
    moment: datetime,
    probe_token: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """New (circuit, window) after recording ``entry``.

    While ``half_open``, only the result carrying the circuit's current
    ``probe_token`` resolves the trial (success closes and resets the window,
    failure reopens); results without a matching token — stale in-flight
    requests issued before the circuit opened — are window bookkeeping only.
    Otherwise the circuit closes -> opens once samples/threshold conditions
    are met; the open -> half_open cooldown transition is owned by
    ``allow_request``.
    """
    if circuit.get("state") == STATE_HALF_OPEN:
        expected = circuit.get("probe_token")
        if probe_token and expected and probe_token == expected:
            if entry["ok"]:
                return _empty_circuit(), [entry]
            return _opened_circuit(moment), [entry]
        return circuit, (window + [entry])[-config["window_size"]:]

    new_window = (window + [entry])[-config["window_size"]:]
    if circuit.get("state") == STATE_CLOSED:
        score = _score_window(new_window)
        if (
            score["samples"] >= config["min_samples"]
            and score["success_rate"] is not None
            and score["success_rate"] < config["open_threshold"]
        ):
            return _opened_circuit(moment), new_window
    return circuit, new_window


def record_result(
    provider: str,
    endpoint_class: str,
    ok: bool,
    latency_ms: float | None = None,
    *,
    now: datetime | None = None,
    probe_token: str | None = None,
) -> dict[str, Any]:
    """Append a request outcome to the rolling window and re-evaluate the circuit."""
    moment = now or _utc_now()
    config = provider_health_settings()
    key = _endpoint_key(endpoint_class)

    def _mutate(document: Any) -> dict[str, Any]:
        doc = document if isinstance(document, dict) else {}
        endpoints = doc.get("endpoints")
        endpoints = dict(endpoints) if isinstance(endpoints, dict) else {}
        endpoint = _load_endpoint(doc, key)

        entry = {"ok": bool(ok), "latency_ms": latency_ms, "at": _now_iso(moment)}
        circuit, window = _resolve_after_result(
            dict(endpoint["circuit"]), endpoint["window"], entry, config, moment, probe_token,
        )

        endpoints[key] = {"window": window, "circuit": circuit}
        doc["endpoints"] = endpoints
        doc["provider"] = provider
        doc["updated_at"] = _now_iso(moment)
        return doc

    return mutate_json(ledger_path(provider), _mutate, default={})


def health_score(provider: str, endpoint_class: str) -> dict[str, Any]:
    """Return window sample count and success rate for (provider, endpoint_class)."""
    document = read_json(ledger_path(provider), {})
    key = _endpoint_key(endpoint_class)
    endpoint = _load_endpoint(document, key)
    score = _score_window(endpoint["window"])
    return {
        "provider": provider,
        "endpoint_class": key,
        "state": endpoint["circuit"].get("state", STATE_CLOSED),
        **score,
    }


def _probe_outcome(circuit: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "allowed": True,
        "state": STATE_HALF_OPEN,
        "reason": reason,
        "probe_token": circuit["probe_token"],
    }


def _admit(
    circuit: dict[str, Any],
    config: dict[str, Any],
    moment: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One admission decision: return (new_circuit, outcome)."""
    state = circuit.get("state", STATE_CLOSED)

    if state == STATE_CLOSED:
        return circuit, {"allowed": True, "state": STATE_CLOSED, "reason": "circuit_closed"}

    if state == STATE_OPEN:
        opened_at = _parse_iso(circuit.get("opened_at"))
        elapsed = (moment - opened_at).total_seconds() if opened_at is not None else None
        if elapsed is not None and elapsed >= config["cooldown_seconds"]:
            claimed = _claimed_probe_circuit(circuit, moment)
            return claimed, _probe_outcome(claimed, "probe_admitted")
        return circuit, {"allowed": False, "state": STATE_OPEN, "reason": "circuit_open"}

    if circuit.get("probe_claimed"):
        claimed_at = _parse_iso(circuit.get("probe_claimed_at"))
        claim_age = (moment - claimed_at).total_seconds() if claimed_at is not None else None
        if claim_age is not None and claim_age < config["probe_ttl_seconds"]:
            return circuit, {"allowed": False, "state": STATE_HALF_OPEN, "reason": "probe_in_flight"}
        # Crashed or lost prober: an expired (or unreadable) claim must not
        # deadlock the breaker forever, so a fresh probe slot is issued.
        claimed = _claimed_probe_circuit(circuit, moment)
        return claimed, _probe_outcome(claimed, "probe_reissued_after_ttl")

    claimed = _claimed_probe_circuit(circuit, moment)
    return claimed, _probe_outcome(claimed, "probe_admitted")


def allow_request(provider: str, endpoint_class: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Decide whether a caller may issue a request, advancing open -> half_open.

    Returns ``{"allowed": bool, "state": ..., "reason": ...}`` plus a
    ``probe_token`` when a half-open probe slot is granted; callers must pass
    that token back to ``record_result`` for the probe outcome to resolve the
    circuit. Only one caller observes ``allowed=True`` per probe TTL window;
    concurrent callers racing the same cooldown expiry all see the same
    atomic mutation.
    """
    moment = now or _utc_now()
    config = provider_health_settings()
    key = _endpoint_key(endpoint_class)
    outcome: dict[str, Any] = {}

    def _mutate(document: Any) -> dict[str, Any]:
        doc = document if isinstance(document, dict) else {}
        endpoints = doc.get("endpoints")
        endpoints = dict(endpoints) if isinstance(endpoints, dict) else {}
        endpoint = _load_endpoint(doc, key)

        circuit, decision = _admit(dict(endpoint["circuit"]), config, moment)
        outcome.update(decision)

        endpoints[key] = {"window": endpoint["window"], "circuit": circuit}
        doc["endpoints"] = endpoints
        doc["provider"] = provider
        doc["updated_at"] = _now_iso(moment)
        return doc

    mutate_json(ledger_path(provider), _mutate, default={})
    return outcome


def summary() -> dict[str, Any]:
    """Health snapshot across every provider that has recorded results."""
    import os

    root = f"{hermes_home()}/runtime/provider_health"
    providers: dict[str, Any] = {}
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            if not name.endswith(".json"):
                continue
            provider = name[: -len(".json")]
            document = read_json(os.path.join(root, name), {})
            endpoints = document.get("endpoints") if isinstance(document, dict) else None
            endpoints = endpoints if isinstance(endpoints, dict) else {}
            providers[provider] = {
                endpoint_class: health_score(provider, endpoint_class)
                for endpoint_class in endpoints
            }
    return {
        "schema": "a_stock_provider_health_summary_v1",
        "generated_at": _now_iso(),
        "providers": providers,
    }
