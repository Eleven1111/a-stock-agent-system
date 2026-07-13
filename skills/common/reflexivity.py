"""Point-in-time reflexivity evidence for A-share candidate decisions.

The model deliberately separates observed facts from actor inference.  It may
reduce risk, but it never admits a positive strategy into live ranking; that
authority remains with ``strategy_registry``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from config_registry import load_registered


SCHEMA = "reflexivity_state_v1"


def _score(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _config_hash(config: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(config), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _open_burst(candidate: Mapping[str, Any], thresholds: Mapping[str, Any]) -> bool:
    value = str(
        candidate.get("first_seal_time")
        or candidate.get("first_seal")
        or ""
    ).strip()
    if len(value) == 4 and value.isdigit():
        value = f"{value[:2]}:{value[2:]}"
    return bool(
        value
        and str(thresholds["open_burst_after"]) < value[:5]
        <= str(thresholds["open_burst_until"])
    )


def _phase_for(
    dominant_state: str,
    sector_state: str,
    crowding: float | None,
    fragility: float | None,
    thresholds: Mapping[str, Any],
    isolated: bool,
) -> str:
    if dominant_state == "S6":
        return "collapse"
    if isolated or sector_state == "weakening":
        return "distribution"
    if (
        crowding is not None and fragility is not None
        and crowding >= float(thresholds["crowding_climax"])
        and fragility >= float(thresholds["fragility_climax"])
    ):
        return "saturation"
    if sector_state == "confirmed":
        return "diffusion"
    if sector_state == "emerging":
        return "ignition"
    return "unknown"


def _actor_probabilities(
    item: Mapping[str, Any], thresholds: Mapping[str, Any], *, burst: bool,
    false_consensus: bool,
) -> tuple[float, float]:
    hot_money = (
        float(thresholds["hot_money_probability"])
        if item.get("hot_money_qualified") else 0.0
    )
    algorithmic = (
        float(thresholds["algorithmic_pattern_probability"])
        if false_consensus
        else float(thresholds["open_burst_pattern_probability"]) if burst else 0.0
    )
    return hot_money, algorithmic


def assess_candidate(
    candidate: Mapping[str, Any] | None,
    selection_state: Mapping[str, Any] | None,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Infer a conservative feedback phase from already-captured evidence."""
    active_config = dict(config or load_registered("reflexivity_strategy"))
    thresholds = dict(active_config.get("thresholds") or {})
    item = dict(candidate or {})
    state = dict(selection_state or {})
    market_state = dict(state.get("market_state") or {})
    crowding_state = dict(state.get("crowding_fragility") or {})
    dominant_state = str(market_state.get("dominant_state") or "")
    crowding = _score(crowding_state.get("crowding_score"))
    fragility = _score(crowding_state.get("fragility_score"))
    sector_state = str(item.get("sector_state") or "")
    ablation = dict(item.get("ablation") or {})
    evidence_count = int(item.get("sector_evidence_count") or 0)
    facts: list[str] = []
    guards: list[str] = []
    if dominant_state:
        facts.append(f"market_state:{dominant_state}")
    if sector_state:
        facts.append(f"sector_state:{sector_state}")
    if item.get("hot_money_qualified"):
        facts.append("hot_money_qualified")
    if ablation.get("structural_leader") is False:
        facts.append("leader_isolated_daily_proxy")
    burst = _open_burst(item, thresholds)
    if burst:
        facts.append("open_burst_0925_0931")
    isolated = (
        int(item.get("leader_rank") or 0) == 1
        and ablation.get("structural_leader") is False
        and sector_state == "weakening"
    )
    if isolated:
        guards.append("leader_isolation_exit_v1")
    false_consensus = (
        burst
        and evidence_count < int(thresholds["multi_source_min"])
        and crowding is not None and crowding >= float(thresholds["crowding_climax"])
        and fragility is not None and fragility >= float(thresholds["fragility_climax"])
    )
    if false_consensus:
        guards.append("algorithmic_false_consensus_guard_v1")
    phase = _phase_for(
        dominant_state, sector_state, crowding, fragility, thresholds, isolated,
    )
    hot_money_probability, algorithmic_probability = _actor_probabilities(
        item, thresholds, burst=burst, false_consensus=false_consensus,
    )
    dominant_actor = "hot_money" if hot_money_probability >= 0.7 else "unknown"
    enough_evidence = bool(dominant_state or sector_state or facts)
    risk_multiplier = (
        0.0 if isolated or dominant_state == "S6"
        else float(thresholds["algorithmic_guard_multiplier"]) if false_consensus
        else 1.0 if enough_evidence
        else 0.0
    )

    return {
        "schema": SCHEMA,
        "strategy_version": str(active_config.get("version") or ""),
        "config_sha256": _config_hash(active_config),
        "status": "ready" if enough_evidence else "insufficient_data",
        "phase": phase,
        "dominant_actor": dominant_actor,
        "actor_probabilities": {
            "hot_money": hot_money_probability,
            "algorithmic_pattern": algorithmic_probability,
        },
        "observed_facts": facts,
        "defensive_guards": guards,
        "risk_multiplier": risk_multiplier,
        "positive_admission": False,
        "live_effect": "defensive_only" if guards else "none",
        "calibrated": False,
    }
