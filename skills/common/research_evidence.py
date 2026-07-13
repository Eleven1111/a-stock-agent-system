"""Bridge Chanlun admission and Serenity research into live policy evidence."""

from __future__ import annotations

import os
import sys
from typing import Any

from deep_research_cache import read_deep_research
from stock_intelligence import read_cache as read_stock_intelligence
import strategy_registry

_CHANLUN_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "chanlun-backtest", "scripts")
)
if _CHANLUN_DIR not in sys.path:
    sys.path.insert(0, _CHANLUN_DIR)
try:
    import chan_structure
except ImportError:
    chan_structure = None


HARD_RISK_DIMENSIONS = {
    "financial_quality": 2,
    "risk_control": 2,
}

_CHAN_DIRECTIONS = {
    "third_buy": "bullish",
    "bottom_divergence": "bullish",
    "third_sell": "bearish",
    "top_divergence": "bearish",
}


def _chanlun_evidence(strategy_id: str) -> dict[str, Any]:
    is_chanlun_strategy = strategy_id.startswith("chanlun_")
    record = strategy_registry.get(strategy_id) if is_chanlun_strategy else None
    if not is_chanlun_strategy:
        status = "no_signal"
    elif not record:
        status = "unregistered"
    elif record.get("gating_status", "enabled") == "disabled":
        status = "disabled"
    elif record.get("allowed_in_live_agent") is True:
        status = "live_allowed"
    else:
        status = "display_only"
    return {
        "selection_strategy_id": strategy_id,
        "strategy_id": strategy_id if is_chanlun_strategy else None,
        "status": status,
        "allowed_in_live_agent": status == "live_allowed",
        "gate_decision": (record or {}).get("gate_decision"),
        "gate_asof": (record or {}).get("gate_asof"),
        "gating_reason": (record or {}).get("gating_reason"),
        "gate_stats": (record or {}).get("gate_stats"),
        "signals": [],
        "live_bullish_signals": [],
        "live_bearish_signals": [],
        "display_only_signals": [],
    }


def _with_chanlun_signals(
    evidence: dict[str, Any],
    bars: list[dict[str, Any]] | None,
    recent_window: int = 10,
) -> dict[str, Any]:
    if not bars or chan_structure is None:
        return evidence
    analysis = chan_structure.analyze(bars)
    signals = []
    total = len(bars)
    for raw in analysis.get("signals") or []:
        signal = dict(raw)
        idx = signal.get("idx")
        if idx is None or idx < total - recent_window:
            continue
        signal["gate_status"] = (
            "live_allowed"
            if strategy_registry.is_allowed_in_live(str(signal.get("strategy_id") or ""))
            else "display_only"
        )
        signal["signal_age_bars"] = total - 1 - int(idx)
        signals.append(signal)
    bearish = {"third_sell", "top_divergence"}
    bullish = {"third_buy", "bottom_divergence"}
    evidence.update({
        "structure_summary": analysis.get("summary"),
        "signals": signals,
        "live_bullish_signals": [
            signal for signal in signals
            if signal["gate_status"] == "live_allowed" and signal.get("type") in bullish
        ],
        "live_bearish_signals": [
            signal for signal in signals
            if signal["gate_status"] == "live_allowed" and signal.get("type") in bearish
        ],
        "display_only_signals": [
            signal for signal in signals if signal["gate_status"] == "display_only"
        ],
    })
    if evidence["live_bullish_signals"] or evidence["live_bearish_signals"]:
        evidence["status"] = "live_allowed"
    elif evidence["display_only_signals"]:
        evidence["status"] = "display_only"
    else:
        evidence["status"] = "no_signal"
    return evidence


def _serenity_evidence(code: str, asof: str | None) -> dict[str, Any]:
    record = read_deep_research(code, today=asof)
    if not record:
        return {
            "available": False,
            "stale": None,
            "hard_risks": [],
        }
    dimensions = record.get("dimensions") or {}
    hard_risks = []
    for dimension, threshold in HARD_RISK_DIMENSIONS.items():
        value = dimensions.get(dimension)
        try:
            score = int(value)
        except (TypeError, ValueError):
            continue
        if score <= threshold:
            hard_risks.append(f"{dimension}={score}/5")
    return {
        "available": True,
        "stale": bool(record.get("stale")),
        "asof": record.get("asof"),
        "age_days": record.get("age_days"),
        "deep_score": record.get("deep_score"),
        "rating": record.get("rating"),
        "valuation_upside_pct": record.get("valuation_upside_pct"),
        "dimensions": dimensions,
        "hard_risks": hard_risks,
        "report_path": record.get("report_path"),
    }


def strategy_attributions(evidence: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract live research signals without replacing the primary strategy.

    These tags support conditional/co-occurrence performance reporting. They
    are not a causal allocation of the recommendation's full return.
    """
    chanlun = (evidence or {}).get("chanlun") or {}
    output = []
    seen = set()
    for bucket in ("live_bullish_signals", "live_bearish_signals"):
        for raw in chanlun.get(bucket) or []:
            strategy_id = str(raw.get("strategy_id") or "").strip()
            signal_type = str(raw.get("type") or "").strip()
            direction = _CHAN_DIRECTIONS.get(signal_type)
            if not strategy_id or not direction:
                continue
            key = (strategy_id, signal_type, raw.get("idx"))
            if key in seen:
                continue
            seen.add(key)
            output.append({
                "strategy_id": strategy_id,
                "role": "research_evidence",
                "direction": direction,
                "signal_type": signal_type,
                "signal_idx": raw.get("idx"),
            })
    return output


def build_research_evidence(
    code: str,
    *,
    strategy_id: str,
    asof: str | None = None,
    bars: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the auditable research evidence supplied to live policy."""
    return {
        "schema": "research_evidence_v1",
        "code": str(code).zfill(6),
        "asof": asof,
        "chanlun": _with_chanlun_signals(_chanlun_evidence(strategy_id), bars),
        "serenity": _serenity_evidence(code, asof),
        "market_intelligence": read_stock_intelligence(code, asof=asof),
    }
