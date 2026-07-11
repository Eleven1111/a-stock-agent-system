"""Research-only event-study and ablation helpers for heuristic features."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _bucket(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    returns = [float(row["return"]) for row in rows]
    return {
        "event_ids": [str(row["event_id"]) for row in rows],
        "count": len(rows),
        "mean_return": sum(returns) / len(returns) if returns else None,
    }


def run_ablation(
    heuristic_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    threshold: float,
    generated_at: str,
) -> dict[str, Any]:
    normalized = [dict(row) for row in rows]
    on = [row for row in normalized if float(row.get("factor") or 0) >= threshold]
    off = [row for row in normalized if float(row.get("factor") or 0) < threshold]
    artifact: dict[str, Any] = {
        "schema": "heuristic_ablation_v1",
        "heuristic_id": str(heuristic_id),
        "threshold": float(threshold),
        "generated_at": str(generated_at),
        "input_sha256": _hash(normalized),
        "factor_on": _bucket(on),
        "factor_off": _bucket(off),
        "status": "research_only",
        "live_effect": "none",
    }
    artifact["artifact_sha256"] = _hash(artifact)
    return artifact


def lhb_review_hint(
    *,
    signal_session: str,
    asof_session: str,
    trading_sessions: Sequence[str],
    max_holding_sessions: int,
) -> dict[str, Any]:
    sessions = list(dict.fromkeys(str(item) for item in trading_sessions))
    try:
        holding = sessions.index(asof_session) - sessions.index(signal_session)
    except ValueError:
        holding = None
    due = holding is not None and holding >= int(max_holding_sessions)
    return {
        "schema": "lhb_review_hint_v1",
        "holding_sessions": holding,
        "review_due": due,
        "action": "review",
        "status": "research_only",
        "live_effect": "none",
    }
