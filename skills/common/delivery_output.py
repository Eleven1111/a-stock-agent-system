"""Helpers for delivery-time output slimming and shadow telemetry."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from . import delivery_policy
    from .paths import hermes_home
    from .state_store import file_lock
except ImportError:  # pragma: no cover - runtime scripts add skills/common to sys.path.
    import delivery_policy  # type: ignore
    from paths import hermes_home  # type: ignore
    from state_store import file_lock  # type: ignore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _telemetry_path() -> Path:
    return Path(hermes_home()) / "cron" / "push_telemetry.jsonl"


def _append_shadow_telemetry(
    *,
    job_id: str,
    output_chars: int,
    reason: str,
    now: datetime | None = None,
) -> None:
    current = now or _now()
    row = {
        "job_id": job_id,
        "trading_date": current.date().isoformat(),
        "delivered": True,
        "output_chars": output_chars,
        "was_compressed": False,
        "silent_reason": "none",
        "would_suppress": True,
        "suppression_reason": reason,
    }
    path = _telemetry_path()
    line = json.dumps(row, ensure_ascii=False, default=str)
    with file_lock(str(path)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def maybe_summarize_text(
    full_text: str,
    summary_text: str,
    *,
    job_id: str,
    has_anomaly: bool,
    policy: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    max_chars: int = 200,
) -> str:
    active_policy = policy or delivery_policy.load_policy()
    if has_anomaly or not delivery_policy.enabled(active_policy, "summary_mode"):
        return full_text
    if delivery_policy.shadow(active_policy, "summary_mode"):
        _append_shadow_telemetry(
            job_id=job_id,
            output_chars=len(full_text),
            reason="summary_mode",
            now=now,
        )
        return full_text
    return summary_text.strip()[:max_chars]


def maybe_summarize_json(
    payload: Mapping[str, Any],
    summary_payload: Mapping[str, Any],
    *,
    job_id: str,
    has_anomaly: bool,
    policy: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    max_chars: int = 200,
) -> str:
    full_text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    active_policy = policy or delivery_policy.load_policy()
    if has_anomaly or not delivery_policy.enabled(active_policy, "summary_mode"):
        return full_text
    if delivery_policy.shadow(active_policy, "summary_mode"):
        _append_shadow_telemetry(
            job_id=job_id,
            output_chars=len(full_text),
            reason="summary_mode",
            now=now,
        )
        return full_text

    summary = dict(summary_payload)
    text = json.dumps(summary, ensure_ascii=False, separators=(",", ":"), default=str)
    while len(text) > max_chars and len(str(summary.get("summary") or "")) > 20:
        summary["summary"] = str(summary.get("summary") or "")[:-20].rstrip() + "…"
        text = json.dumps(summary, ensure_ascii=False, separators=(",", ":"), default=str)
    return text
