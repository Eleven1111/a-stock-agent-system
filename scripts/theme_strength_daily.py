#!/usr/bin/env python3
"""Deterministic daily theme-strength review (§4b/§4c driver).

Reads facts the DAG already produced — the candidate-discovery input snapshot
(full-universe quotes + lianban ladder + capital-flow lineage in
signal_context) — computes each live theme's strength record, runs the pure
lifecycle FSM, persists the daily history, applies stage transitions to the
registry, and emits a bounded artifact. When a theme enters ``mainline`` it
enqueues a single ``theme_review`` research task (dedup/cooldown enforced by the
research bus).

Runs as a plain cron command (no model turn). Fail-closed throughout: missing
snapshot / universe basis degrades dimensions to ``unavailable`` and never
fabricates data. No model name or vendor referenced.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from typing import Any, Mapping

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
if COMMON not in sys.path:
    sys.path.insert(0, COMMON)

import theme_registry  # noqa: E402
import theme_strength  # noqa: E402
from market_snapshot import read_snapshot  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import read_json  # noqa: E402


def _load_discovery_inputs(asof: str) -> dict[str, Any] | None:
    """Resolve the latest candidate-discovery-input snapshot payload via the
    candidate pool's lineage ref. Returns None (fail-closed) when unavailable."""
    pool = read_json(data_file("stock-triage", "candidate_pool_latest.json"), {})
    if not isinstance(pool, dict):
        return None
    ref = pool.get("input_snapshot") or {}
    path = ref.get("snapshot_path")
    if not path or not os.path.exists(str(path)):
        return None
    try:
        record = read_snapshot(str(path))
    except (ValueError, OSError):
        return None
    payload = record.get("payload") or {}
    if str(payload.get("schema") or "") != "candidate_discovery_inputs_v1":
        return None
    if asof and str(pool.get("asof") or "") not in ("", asof) and str(record.get("trading_date")) != asof:
        # Stale lineage relative to the requested trading date -> fail closed.
        return None
    return {
        "quotes": payload.get("quotes") or {},
        "signal_context": payload.get("signal_context") or {},
        "trading_date": record.get("trading_date"),
    }


def _enqueue_theme_review(theme: Mapping[str, Any], trading_date: str) -> dict[str, Any] | None:
    """Enqueue a theme_review task when a theme enters mainline (§4 trigger).
    Isolated so a research-bus outage degrades to "not enqueued"."""
    try:
        import research_bus
    except ImportError:
        return None
    subject = {"theme": theme.get("name"), "theme_id": theme.get("id")}
    try:
        outcome = research_bus.enqueue_task(
            "theme_review",
            subject,
            reason="theme_entered_mainline",
            trigger={
                "source": "theme_strength_daily",
                "theme_id": theme.get("id"),
                "stage": "mainline",
            },
            trading_date=trading_date,
            config=research_bus.load_config(),
        )
    except Exception:  # noqa: BLE001 - trigger must not break the strength report
        return None
    if outcome.get("enqueued"):
        research_bus.append_ledger_event({
            "event_type": "research.enqueued",
            "task_id": outcome["task"]["id"],
            "kind": "theme_review",
            "reason": "theme_entered_mainline",
            "trading_date": trading_date,
        })
        return {"theme_id": theme.get("id"), "task_id": outcome["task"]["id"], "enqueued": True}
    return {"theme_id": theme.get("id"), "enqueued": False, "skip_reason": outcome.get("reason")}


def run(*, trading_date: str) -> dict[str, Any]:
    themes = theme_registry.active_themes(trading_date)
    inputs = _load_discovery_inputs(trading_date)
    if inputs is None:
        return {
            "schema": theme_strength.SCHEMA,
            "status": "no_inputs",
            "trading_date": trading_date,
            "reason": "candidate_discovery_input_snapshot_unavailable",
            "active_theme_count": len(themes),
            "themes": [],
            "has_signal": False,
        }

    quotes = inputs["quotes"]
    signal_ctx = inputs["signal_context"] or {}
    ladder = signal_ctx.get("lianban_ladder") or {}
    stock_flows = signal_ctx.get("stock_flows") or {}
    market_median = theme_strength.market_median_return(quotes)

    history = theme_strength.load_history()
    results: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    lifecycle_cfg = _load_lifecycle_config()

    for theme in themes:
        theme_id = str(theme.get("id"))
        prior_excess = theme_strength.prior_excess_series(theme_id, history=history)
        record = theme_strength.build_theme_record(
            theme,
            asof=trading_date,
            quotes_by_code=quotes,
            ladder=ladder,
            stock_flows=stock_flows,
            market_median=market_median,
            prior_excess_series=prior_excess,
        )
        strong_flags = [*theme_strength.prior_strong_flags(theme_id, history=history),
                        bool(record["is_strong"])]
        persistence = theme_strength.compute_persistence(strong_flags)
        record["persistence"] = persistence
        prior = theme_strength.theme_history(theme_id, history=history)
        prior_ladder = (
            int(((prior[-1].get("breadth") or {}).get("ladder_height")) or 0)
            if prior else None
        )
        wk = theme_strength.weak_streak(theme_id, include_today=record["is_strong"], history=history)
        decision = theme_strength.decide_stage(
            str(theme.get("status") or "emerging"),
            record,
            persistence=persistence,
            prior_ladder_height=prior_ladder,
            weak_streak=wk,
            config=lifecycle_cfg,
        )
        record["stage_decision"] = decision
        theme_strength.append_history(theme_id, record)

        prior_stage = str(theme.get("status") or "emerging")
        new_stage = decision["stage"]
        if new_stage != prior_stage:
            theme_registry.set_stage(theme_id, new_stage, reason=decision["reason"])
            if new_stage == "mainline":
                review = _enqueue_theme_review(theme, trading_date)
                if review:
                    reviews.append(review)
        results.append({
            "theme_id": theme_id,
            "name": theme.get("name"),
            "prior_stage": prior_stage,
            "stage": new_stage,
            "reason": decision["reason"],
            "is_strong": record["is_strong"],
            "persistence": persistence,
            "breadth": record["breadth"],
            "capital": record["capital"],
            "relative_strength": record["relative_strength"],
        })

    transitions = [r for r in results if r["prior_stage"] != r["stage"]]
    return {
        "schema": theme_strength.SCHEMA,
        "status": "ok",
        "trading_date": trading_date,
        "market_median_available": market_median is not None,
        "active_theme_count": len(themes),
        "transitions": transitions,
        "theme_reviews_enqueued": [r for r in reviews if r.get("enqueued")],
        "themes": results,
        "has_signal": bool(transitions or results),
    }


def _load_lifecycle_config() -> dict[str, Any]:
    """Lifecycle thresholds from candidate_selection.json (theme section), or
    module defaults. All thresholds are config-driven, never hardcoded here."""
    path = os.environ.get("A_STOCK_CANDIDATE_SELECTION_CONFIG") or os.path.join(
        ROOT, "config", "candidate_selection.json"
    )
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    section = (payload.get("theme_weighting") or {}) if isinstance(payload, dict) else {}
    lifecycle = section.get("lifecycle") if isinstance(section, dict) else None
    return dict(lifecycle) if isinstance(lifecycle, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="每日主题强度日评（确定性）")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--trading-date")
    args = parser.parse_args()
    result = run(trading_date=args.trading_date or date.today().isoformat())
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
