#!/usr/bin/env python3
"""Research-only runtime for the 14:35/14:50 tail-close strategy lane."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from paths import data_file  # noqa: E402
from decision_policy import evaluate_decision  # noqa: E402
from portfolio_policy import coordinate_research_allocations  # noqa: E402
import signal_ledger  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402
from tail_close_strategy import (  # noqa: E402
    AFTER_HOURS_STRATEGY_ID,
    TailCloseContractError,
    build_prepared_state,
    build_research_decision,
    canonical_hash,
    label_d1_outcome,
    load_tail_config,
    simulate_after_hours_fixed_fill,
    simulate_continuous_fill,
    validate_prepared_state,
)
from tail_close_validation import (  # noqa: E402
    evaluate_manual_pilot_eligibility,
    evaluate_oos_family,
    evaluate_shadow_readiness,
    strategy_family_config_hash,
)
from validation_program import load_validation_thresholds  # noqa: E402


def _state_path(kind: str, trading_date: str) -> str:
    return data_file("stock-triage", f"tail_close/{kind}/{trading_date}.json")


def _default_input(kind: str) -> str:
    return data_file("stock-triage", f"tail_close/input/{kind}_latest.json")


def _read_mapping(path: str) -> dict[str, Any]:
    payload = read_json(path, None)
    if not isinstance(payload, Mapping):
        raise TailCloseContractError(f"input_missing_or_invalid:{path}")
    return dict(payload)


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_date(payload: Mapping[str, Any], explicit: str | None) -> str:
    value = str(explicit or payload.get("trading_date") or "")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise TailCloseContractError("trading_date_invalid") from exc


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def _blocked(command: str, error: Exception) -> dict[str, Any]:
    return {
        "schema": "tail_close_runtime_status_v1",
        "command": command,
        "status": "blocked",
        "reason": str(error),
        "signals": [],
        "has_signal": False,
        "research_only": True,
        "live_weight": 0.0,
        "automatic_order_count": 0,
        "broker_call_count": 0,
    }


def _append_research_signals(decision: Mapping[str, Any]) -> int:
    events = []
    for signal in decision.get("signals") or []:
        links = signal_ledger.make_links(
            correlation_id=f"tail-close:{decision['trading_date']}",
            signal_id=str(signal["signal_id"]),
        )
        events.append(signal_ledger.research_signal_event(signal, links))
    return len(signal_ledger.append_events(events))


def _apply_shared_governance(
    decision: dict[str, Any],
    bundle: Mapping[str, Any],
    config: Mapping[str, Any],
) -> None:
    tail_signals = list(decision.get("signals") or [])
    if not tail_signals:
        decision["portfolio_coordination"] = {
            "schema": "portfolio_research_coordination_v1",
            "allocations": [],
            "rejections": [],
            "standalone_count": 0,
            "incremental_count": 0,
        }
        return
    capacity = bundle.get("security_capacity_by_code")
    if not isinstance(capacity, Mapping):
        raise TailCloseContractError("shared_security_capacity_missing")
    sibling_signals = bundle.get("shared_research_signals") or []
    if not isinstance(sibling_signals, list) or any(
        not isinstance(item, Mapping) for item in sibling_signals
    ):
        raise TailCloseContractError("shared_research_signals_invalid")
    coordination = coordinate_research_allocations(
        [*sibling_signals, *tail_signals],
        security_capacity=capacity,
        max_single_pct=float(
            config["portfolio"]["maximum_single_position_pct"]
        ),
        max_sector_pct=float(
            config["portfolio"]["maximum_sector_exposure_pct"]
        ),
    )
    allocations = {
        str(item["signal_id"]): item
        for item in coordination["allocations"]
        if item.get("strategy_id") == decision["strategy_id"]
    }
    governed = []
    for signal in tail_signals:
        allocation = allocations.get(str(signal["signal_id"]))
        if allocation is None:
            continue
        policy = evaluate_decision(
            requested_action="buy",
            quality_report={"status": "passed"},
            strategy_record=None,
            market_regime=bundle.get("market") or {},
            portfolio_risk={"allowed": True, "reasons": []},
            research_evidence=None,
            strategy_lane="tail_close",
            raw_score=float(signal.get("score") or 0),
        )
        governed.append(
            {
                **signal,
                "portfolio_allocation": allocation,
                "live_policy_decision": policy,
            }
        )
    decision["signals_before_portfolio"] = len(tail_signals)
    decision["signals"] = governed
    decision["portfolio_coordination"] = coordination
    if not governed:
        decision["status"] = "no_action_portfolio"
    decision.pop("decision_hash", None)
    decision["decision_hash"] = canonical_hash(decision)


def run_prepare(args: argparse.Namespace) -> dict[str, Any]:
    config = load_tail_config(args.config)
    bundle = _read_mapping(args.input or _default_input("prepare"))
    trading_date = _safe_date(bundle, args.trading_date)
    prepared = build_prepared_state(bundle, config)
    output = args.output or _state_path("prepared", trading_date)
    atomic_write_json(output, prepared)
    return {
        **prepared,
        "artifact_path": output,
        "has_signal": False,
        "broker_call_count": 0,
        "automatic_order_count": 0,
    }


def run_decision(args: argparse.Namespace) -> dict[str, Any]:
    config = load_tail_config(args.config)
    bundle = _read_mapping(args.input or _default_input("decision"))
    trading_date = _safe_date(bundle, args.trading_date)
    output = args.output or _state_path("decisions", trading_date)
    prepared_path = args.prepared or _state_path("prepared", trading_date)
    prepared = read_json(prepared_path, None)
    validate_prepared_state(
        prepared if isinstance(prepared, Mapping) else None,
        bundle,
        config,
    )
    existing = read_json(output, None)
    if (
        isinstance(existing, Mapping)
        and existing.get("input_hash") == canonical_hash(bundle)
        and existing.get("config_hash") == canonical_hash(config)
    ):
        existing_core = {
            key: value
            for key, value in existing.items()
            if key != "runtime_artifact_hash"
        }
        if existing.get("runtime_artifact_hash") != canonical_hash(existing_core):
            raise TailCloseContractError("canonical_decision_hash_mismatch")
        return {**dict(existing), "artifact_path": output, "reused": True}
    decision = build_research_decision(
        bundle,
        config,
        prepared_state=prepared if isinstance(prepared, Mapping) else None,
        emitted_at=args.emitted_at,
    )
    _apply_shared_governance(decision, bundle, config)
    decision["input_hash"] = canonical_hash(bundle)
    decision["config_hash"] = canonical_hash(config)
    decision["ledger_events_appended"] = _append_research_signals(decision)
    decision["has_signal"] = bool(decision.get("signals"))
    decision["runtime_artifact_hash"] = canonical_hash(decision)
    atomic_write_json(output, decision)
    return {**decision, "artifact_path": output, "reused": False}


def run_reconcile(args: argparse.Namespace) -> dict[str, Any]:
    config = load_tail_config(args.config)
    payload = _read_mapping(args.input or _default_input("reconcile"))
    decision = payload.get("decision")
    bars_by_code = payload.get("bars_by_code")
    if not isinstance(decision, Mapping) or not isinstance(bars_by_code, Mapping):
        raise TailCloseContractError("reconcile_payload_invalid")
    fills = []
    events = []
    for signal in decision.get("signals") or []:
        code = str(signal.get("code") or "").zfill(6)
        bars = bars_by_code.get(code)
        if not isinstance(bars, list):
            raise TailCloseContractError(f"reconcile_bars_missing:{code}")
        fill = simulate_continuous_fill(
            signal,
            bars,
            config,
            decision_emitted_at=str(decision["decision_emitted_at"]),
        )
        fills.append(fill)
        links = signal_ledger.make_links(
            correlation_id=f"tail-close:{decision['trading_date']}",
            signal_id=str(signal["signal_id"]),
        )
        events.extend(
            [
                signal_ledger.simulated_order_event(
                    {
                        **signal,
                        "simulation": True,
                        "requested_quantity": fill["requested_quantity"],
                    },
                    links,
                ),
                signal_ledger.simulated_fill_event(
                    {**fill, "provenance": signal["provenance"]},
                    links,
                ),
                signal_ledger.simulation_reconciliation_event(
                    {
                        "strategy_id": signal["strategy_id"],
                        "signal_id": signal["signal_id"],
                        "trading_date": decision["trading_date"],
                        "status": fill["status"],
                        "requested_quantity": fill["requested_quantity"],
                        "filled_quantity": fill["filled_quantity"],
                        "unfilled_quantity": fill["unfilled_quantity"],
                        "decision_hash": decision["decision_hash"],
                        "fill_hash": fill["fill_hash"],
                        "provenance": signal["provenance"],
                        "simulation": True,
                    },
                    links,
                ),
            ]
        )
    appended = signal_ledger.append_events(events)
    trading_date = str(decision["trading_date"])
    result = {
        "schema": "tail_close_reconciliation_v1",
        "strategy_id": decision["strategy_id"],
        "trading_date": trading_date,
        "status": "simulated",
        "fills": fills,
        "ledger_events_appended": len(appended),
        "research_only": True,
        "live_weight": 0.0,
        "broker_call_count": 0,
        "automatic_order_count": 0,
        "has_signal": bool(fills),
    }
    result["artifact_hash"] = canonical_hash(result)
    output = args.output or _state_path("reconciliations", trading_date)
    atomic_write_json(output, result)
    return {**result, "artifact_path": output}


def run_label_outcome(args: argparse.Namespace) -> dict[str, Any]:
    config = load_tail_config(args.config)
    payload = _read_mapping(args.input or _default_input("outcome"))
    fill = payload.get("fill")
    sessions = payload.get("sessions")
    if not isinstance(fill, Mapping) or not isinstance(sessions, list):
        raise TailCloseContractError("outcome_payload_invalid")
    outcome = label_d1_outcome(fill, sessions, config)
    trading_date = str(fill["trading_date"])
    output = args.output or _state_path(
        "outcomes",
        f"{trading_date}-{fill.get('signal_id')}",
    )
    atomic_write_json(output, outcome)
    return {**outcome, "artifact_path": output, "has_signal": False}


def run_after_hours_shadow(args: argparse.Namespace) -> dict[str, Any]:
    config = load_tail_config(args.config)
    sibling = config["strategies"][AFTER_HOURS_STRATEGY_ID]
    if (
        sibling.get("enabled") is not True
        or sibling.get("readiness") != "ready"
    ):
        return {
            "schema": "tail_close_after_hours_capability_v1",
            "strategy_id": AFTER_HOURS_STRATEGY_ID,
            "status": "not_ready",
            "reason": "forward_queue_capability_not_approved",
            "has_signal": False,
            "research_only": True,
            "live_weight": 0.0,
            "broker_call_count": 0,
            "automatic_order_count": 0,
        }
    payload = _read_mapping(args.input or _default_input("after_hours_signal"))
    signal = payload.get("signal")
    if not isinstance(signal, Mapping):
        raise TailCloseContractError("after_hours_signal_payload_invalid")
    if signal.get("strategy_id") != AFTER_HOURS_STRATEGY_ID:
        raise TailCloseContractError("after_hours_strategy_id_invalid")
    trading_date = str(signal["trading_date"])
    frozen = {
        "schema": "tail_close_after_hours_frozen_signal_v1",
        "strategy_id": AFTER_HOURS_STRATEGY_ID,
        "trading_date": trading_date,
        "decision_time": f"{trading_date}T15:05:00+08:00",
        "signal": dict(signal),
        "signal_hash": canonical_hash(signal),
        "status": "frozen",
        "research_only": True,
        "live_weight": 0.0,
        "broker_call_count": 0,
        "automatic_order_count": 0,
        "has_signal": True,
    }
    output = args.output or _state_path("after_hours", trading_date)
    atomic_write_json(output, frozen)
    return {**frozen, "artifact_path": output}


def run_after_hours_reconcile(args: argparse.Namespace) -> dict[str, Any]:
    config = load_tail_config(args.config)
    sibling = config["strategies"][AFTER_HOURS_STRATEGY_ID]
    if (
        sibling.get("enabled") is not True
        or sibling.get("readiness") != "ready"
    ):
        raise TailCloseContractError(
            "after_hours_forward_queue_capability_not_approved"
        )
    payload = _read_mapping(args.input or _default_input("after_hours_reconcile"))
    frozen = payload.get("frozen_signal")
    observations = payload.get("observations")
    if not isinstance(frozen, Mapping) or not isinstance(observations, list):
        raise TailCloseContractError("after_hours_reconcile_payload_invalid")
    if frozen.get("schema") != "tail_close_after_hours_frozen_signal_v1":
        raise TailCloseContractError("after_hours_frozen_signal_invalid")
    signal = frozen.get("signal")
    if not isinstance(signal, Mapping):
        raise TailCloseContractError("after_hours_frozen_signal_invalid")
    if frozen.get("signal_hash") != canonical_hash(signal):
        raise TailCloseContractError("after_hours_frozen_signal_hash_mismatch")
    result = simulate_after_hours_fixed_fill(signal, observations, config)
    trading_date = str(signal["trading_date"])
    output = args.output or _state_path("after_hours_reconciliations", trading_date)
    atomic_write_json(output, result)
    return {**result, "artifact_path": output, "has_signal": False}


def run_manual_reconcile(args: argparse.Namespace) -> dict[str, Any]:
    config = load_tail_config(args.config)
    if config["safety"].get("manual_pilot_reconciliation_enabled") is not True:
        raise TailCloseContractError("manual_reconciliation_not_enabled")
    payload = _read_mapping(args.input or _default_input("manual_reconcile"))
    fill, manual = _manual_execution_inputs(payload)
    strategy_id = str(fill.get("strategy_id") or "")
    verified_oos, verified_shadow = _verify_manual_pilot_evidence(
        payload=payload,
        config=config,
        strategy_id=strategy_id,
    )
    approval = _verify_manual_approval(
        payload=payload,
        strategy_id=strategy_id,
        verified_oos=verified_oos,
        verified_shadow=verified_shadow,
    )
    pilot = _verify_manual_pilot_eligibility(
        strategy_id,
        verified_oos,
        verified_shadow,
    )
    provenance = _verify_manual_fill(fill)
    signal_id = str(fill.get("signal_id") or "")
    trading_date = str(fill.get("trading_date") or "")
    links = _verify_manual_ledger(fill, signal_id, trading_date)
    evidence_hash = _file_sha256(str(manual.get("evidence_path") or ""))
    record = _manual_reconciliation_record(
        fill=fill,
        manual=manual,
        approval=approval,
        provenance=provenance,
        pilot=pilot,
        evidence_hash=evidence_hash,
    )
    appended = signal_ledger.append_events(
        [signal_ledger.manual_reconciliation_event(record, links)]
    )
    result = _manual_reconciliation_result(record, len(appended))
    output = args.output or _state_path(
        "manual_reconciliations",
        f"{trading_date}-{signal_id}",
    )
    atomic_write_json(output, result)
    return {**result, "artifact_path": output}


def _manual_execution_inputs(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    fill = payload.get("simulation_fill")
    manual = payload.get("manual_execution")
    if not isinstance(fill, Mapping) or not isinstance(manual, Mapping):
        raise TailCloseContractError("manual_reconcile_payload_invalid")
    return dict(fill), dict(manual)


def _verify_manual_pilot_evidence(
    *,
    payload: Mapping[str, Any],
    config: Mapping[str, Any],
    strategy_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    oos_result = _read_mapping(str(payload.get("oos_result_path") or ""))
    shadow_result = _read_mapping(str(payload.get("shadow_result_path") or ""))
    oos_dataset_path = str(payload.get("oos_dataset_path") or "")
    oos_dataset = _read_mapping(oos_dataset_path)
    oos_evaluation = _read_mapping(
        str(payload.get("oos_evaluation_input_path") or "")
    )
    shadow_observations = _read_mapping(
        str(payload.get("shadow_observations_path") or "")
    )
    if (
        oos_result.get("config_hash")
        != strategy_family_config_hash(config, strategy_id)
    ):
        raise TailCloseContractError("manual_oos_config_mismatch")
    outcomes = oos_dataset.get("outcomes")
    variant_returns = oos_evaluation.get("variant_returns")
    observations = shadow_observations.get("observations")
    if (
        not isinstance(outcomes, list)
        or not isinstance(variant_returns, Mapping)
        or not isinstance(observations, list)
    ):
        raise TailCloseContractError("manual_validation_evidence_invalid")
    thresholds_path = (
        Path(__file__).resolve().parents[3]
        / "config"
        / "validation_thresholds.json"
    )
    thresholds = load_validation_thresholds(thresholds_path)
    verified_oos = evaluate_oos_family(
        strategy_id=strategy_id,
        outcomes=outcomes,
        variant_returns=variant_returns,
        precommit_registry_path=str(
            payload.get("precommit_registry_path") or ""
        ),
        precommit_id=str(oos_result.get("precommit_id") or ""),
        reveal_record_sha256=str(
            oos_result.get("reveal_record_sha256") or ""
        ),
        dataset_path=oos_dataset_path,
        validation_thresholds_path=thresholds_path,
        strategy_config=config,
        validation_thresholds=thresholds,
    )
    if verified_oos.get("artifact_hash") != oos_result.get("artifact_hash"):
        raise TailCloseContractError("manual_oos_artifact_not_reproducible")
    verified_shadow = evaluate_shadow_readiness(
        strategy_id=strategy_id,
        observations=observations,
        oos_result=verified_oos,
        strategy_config=config,
        validation_thresholds=thresholds,
    )
    if verified_shadow.get("artifact_hash") != shadow_result.get("artifact_hash"):
        raise TailCloseContractError("manual_shadow_artifact_not_reproducible")
    return verified_oos, verified_shadow


def _verify_manual_approval(
    *,
    payload: Mapping[str, Any],
    strategy_id: str,
    verified_oos: Mapping[str, Any],
    verified_shadow: Mapping[str, Any],
) -> dict[str, Any]:
    approval = _read_mapping(str(payload.get("human_approval_path") or ""))
    approval_core = {
        key: value
        for key, value in approval.items()
        if key != "artifact_hash"
    }
    if approval.get("artifact_hash") != canonical_hash(approval_core):
        raise TailCloseContractError("manual_human_approval_hash_mismatch")
    if (
        approval.get("schema") != "tail_close_human_approval_v1"
        or approval.get("strategy_id") != strategy_id
        or approval.get("explicit_human_approval") is not True
        or approval.get("oos_artifact_hash") != verified_oos.get("artifact_hash")
        or approval.get("shadow_artifact_hash")
        != verified_shadow.get("artifact_hash")
    ):
        raise TailCloseContractError("manual_human_approval_invalid")
    return approval


def _verify_manual_pilot_eligibility(
    strategy_id: str,
    verified_oos: Mapping[str, Any],
    verified_shadow: Mapping[str, Any],
) -> dict[str, Any]:
    pilot = evaluate_manual_pilot_eligibility(
        strategy_id=strategy_id,
        oos_result=verified_oos,
        shadow_result=verified_shadow,
        explicit_human_approval=True,
    )
    if pilot["status"] != "eligible_for_manual_pilot":
        raise TailCloseContractError("manual_pilot_not_eligible")
    return pilot


def _verify_manual_fill(fill: Mapping[str, Any]) -> dict[str, Any]:
    fill_core = {
        key: value
        for key, value in fill.items()
        if key != "fill_hash"
    }
    if fill.get("fill_hash") != canonical_hash(fill_core):
        raise TailCloseContractError("manual_simulation_fill_hash_mismatch")
    provenance = fill.get("provenance")
    if not isinstance(provenance, Mapping):
        raise TailCloseContractError("manual_reconcile_provenance_missing")
    return dict(provenance)


def _verify_manual_ledger(
    fill: Mapping[str, Any],
    signal_id: str,
    trading_date: str,
) -> dict[str, Any]:
    links = signal_ledger.make_links(
        correlation_id=f"tail-close:{trading_date}",
        signal_id=signal_id,
    )
    lifecycle = next(
        (
            item
            for item in signal_ledger.project_tail_close_lifecycle(
                ledger_file=signal_ledger.LEDGER_FILE
            )
            if item.get("signal_id") == signal_id
        ),
        None,
    )
    ledger_fill = (
        ((lifecycle or {}).get("stages") or {}).get("fill") or {}
    ).get("payload") or {}
    if (
        not lifecycle
        or lifecycle.get("complete") is not True
        or lifecycle.get("violations")
        or ledger_fill.get("fill_hash") != fill.get("fill_hash")
    ):
        raise TailCloseContractError("manual_ledger_lifecycle_not_reconciled")
    return links


def _manual_reconciliation_record(
    *,
    fill: Mapping[str, Any],
    manual: Mapping[str, Any],
    approval: Mapping[str, Any],
    provenance: Mapping[str, Any],
    pilot: Mapping[str, Any],
    evidence_hash: str,
) -> dict[str, Any]:
    return {
        "strategy_id": str(fill.get("strategy_id") or ""),
        "signal_id": str(fill.get("signal_id") or ""),
        "trading_date": str(fill.get("trading_date") or ""),
        "status": "reconciled",
        "pilot_gate_hash": pilot["artifact_hash"],
        "simulation_fill_hash": fill["fill_hash"],
        "evidence_hash": evidence_hash,
        "human_approval_id": approval.get("approval_id"),
        "human_approved_at": approval.get("approved_at"),
        "actual_filled_quantity": manual.get("actual_filled_quantity"),
        "actual_fill_price": manual.get("actual_fill_price"),
        "external_broker_evidence_confirmed": (
            manual.get("external_broker_evidence_confirmed") is True
        ),
        "provenance": dict(provenance),
        "research_only": True,
        "live_weight": 0.0,
        "automatic_order_count": 0,
        "broker_call_count": 0,
    }


def _manual_reconciliation_result(
    record: Mapping[str, Any],
    appended_count: int,
) -> dict[str, Any]:
    result = {
        "schema": "tail_close_manual_reconciliation_v1",
        "strategy_id": record["strategy_id"],
        "signal_id": record["signal_id"],
        "trading_date": record["trading_date"],
        "status": "reconciled",
        "pilot_gate_hash": record["pilot_gate_hash"],
        "simulation_fill_hash": record["simulation_fill_hash"],
        "ledger_events_appended": appended_count,
        "system_ordering": "forbidden",
        "human_decision_and_order_required": True,
        "research_only": True,
        "live_weight": 0.0,
        "automatic_order_count": 0,
        "broker_call_count": 0,
        "has_signal": False,
    }
    result["artifact_hash"] = canonical_hash(result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "prepare",
            "decision",
            "reconcile",
            "label-outcome",
            "manual-reconcile",
            "after-hours-shadow",
            "after-hours-reconcile",
        ),
    )
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--prepared")
    parser.add_argument("--config")
    parser.add_argument("--trading-date")
    parser.add_argument("--emitted-at")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runners = {
        "prepare": run_prepare,
        "decision": run_decision,
        "reconcile": run_reconcile,
        "label-outcome": run_label_outcome,
        "manual-reconcile": run_manual_reconcile,
        "after-hours-shadow": run_after_hours_shadow,
        "after-hours-reconcile": run_after_hours_reconcile,
    }
    try:
        result = runners[args.command](args)
    except (OSError, ValueError, TailCloseContractError) as exc:
        _emit(_blocked(args.command, exc))
        return 2
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
