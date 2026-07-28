import json

import signal_ledger
import tail_close_signal

from tail_close_test_support import TRADING_DATE, bundle, config
from tail_close_strategy import canonical_hash
from tail_close_validation import strategy_family_config_hash


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _artifact(payload):
    result = dict(payload)
    result["artifact_hash"] = canonical_hash(result)
    return result


def test_prepare_decision_reconcile_are_idempotent_and_research_only(
    tmp_path,
    monkeypatch,
    capsys,
):
    ledger = tmp_path / "signal_ledger.jsonl"
    monkeypatch.setattr(signal_ledger, "LEDGER_FILE", str(ledger))
    prepare_input = _write(tmp_path / "prepare.json", bundle(prepare=True))
    decision_input = _write(tmp_path / "decision.json", bundle())
    prepared = tmp_path / "prepared.json"
    decision = tmp_path / "decision-output.json"

    assert (
        tail_close_signal.main(
            [
                "prepare",
                "--input",
                prepare_input,
                "--output",
                str(prepared),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    decision_args = [
        "decision",
        "--input",
        decision_input,
        "--prepared",
        str(prepared),
        "--output",
        str(decision),
        "--emitted-at",
        f"{TRADING_DATE}T14:50:10+08:00",
        "--json",
    ]
    assert tail_close_signal.main(decision_args) == 0
    first = json.loads(capsys.readouterr().out)
    assert tail_close_signal.main(decision_args) == 0
    second = json.loads(capsys.readouterr().out)

    assert first["has_signal"] is True
    assert first["live_weight"] == 0
    assert first["broker_call_count"] == 0
    assert first["signals"][0]["portfolio_allocation"]["allocated_capacity"] == 100_000
    assert first["signals"][0]["live_policy_decision"]["decision"] == "watch"
    assert first["signals"][0]["live_policy_decision"]["position_multiplier"] == 0
    assert second["reused"] is True
    assert len(signal_ledger.read_events(str(ledger))) == 1

    reconcile_input = _write(
        tmp_path / "reconcile.json",
        {
            "decision": first,
            "bars_by_code": {
                "600001": [
                    {
                        "event_time": f"{TRADING_DATE}T14:50:11+08:00",
                        "available_time": f"{TRADING_DATE}T14:50:11+08:00",
                        "ask_price": 10.31,
                        "available_sell_volume": 1_000_000,
                    }
                ]
            },
        },
    )
    assert (
        tail_close_signal.main(
            [
                "reconcile",
                "--input",
                reconcile_input,
                "--output",
                str(tmp_path / "reconciliation.json"),
                "--json",
            ]
        )
        == 0
    )
    reconciled = json.loads(capsys.readouterr().out)
    events = signal_ledger.read_events(str(ledger))

    assert reconciled["status"] == "simulated"
    assert reconciled["broker_call_count"] == 0
    assert [event["event_type"] for event in events] == [
        "tail_close.signal_created",
        "tail_close.order_simulated",
        "tail_close.fill_simulated",
        "tail_close.simulation_reconciled",
    ]
    lifecycle = signal_ledger.project_tail_close_lifecycle(ledger_file=str(ledger))
    assert len(lifecycle) == 1
    assert lifecycle[0]["complete"] is True
    assert lifecycle[0]["violations"] == []

    assert (
        tail_close_signal.main(
            [
                "reconcile",
                "--input",
                reconcile_input,
                "--output",
                str(tmp_path / "reconciliation.json"),
                "--json",
            ]
        )
        == 0
    )
    retried = json.loads(capsys.readouterr().out)
    assert retried["ledger_events_appended"] == 0
    assert len(signal_ledger.read_events(str(ledger))) == 4

    manual_input = _write(
        tmp_path / "manual-reconcile.json",
        {
            "simulation_fill": reconciled["fills"][0],
            "manual_execution": {
                "explicit_human_approval": True,
                "human_approval_id": "approval-test-1",
                "human_approved_at": f"{TRADING_DATE}T16:00:00+08:00",
                "evidence_hash": "e" * 64,
                "actual_filled_quantity": 1_000,
                "actual_fill_price": 10.31,
                "external_broker_evidence_confirmed": True,
            },
        },
    )
    assert (
        tail_close_signal.main(
            [
                "manual-reconcile",
                "--input",
                manual_input,
                "--output",
                str(tmp_path / "manual-reconciliation.json"),
                "--json",
            ]
        )
        == 2
    )
    manual = json.loads(capsys.readouterr().out)
    assert manual["status"] == "blocked"
    assert manual["reason"] == "manual_reconciliation_not_enabled"
    assert manual["broker_call_count"] == 0
    assert len(signal_ledger.read_events(str(ledger))) == 4


def test_shared_capacity_deduplicates_tail_signal_against_higher_priority_lane(
    tmp_path,
    monkeypatch,
    capsys,
):
    ledger = tmp_path / "signal_ledger.jsonl"
    monkeypatch.setattr(signal_ledger, "LEDGER_FILE", str(ledger))
    prepare_input = _write(tmp_path / "prepare.json", bundle(prepare=True))
    decision_bundle = bundle()
    decision_bundle["shared_research_signals"] = [
        {
            "signal_id": "trend-600001",
            "strategy_id": "trend:v1",
            "code": "600001",
            "sector": "S0",
            "priority": 2.0,
            "requested_capacity": 100_000,
            "proposed_position_pct": 10.0,
        }
    ]
    decision_input = _write(tmp_path / "decision.json", decision_bundle)
    prepared = tmp_path / "prepared.json"

    assert (
        tail_close_signal.main(
            [
                "prepare",
                "--input",
                prepare_input,
                "--output",
                str(prepared),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        tail_close_signal.main(
            [
                "decision",
                "--input",
                decision_input,
                "--prepared",
                str(prepared),
                "--output",
                str(tmp_path / "decision-output.json"),
                "--emitted-at",
                f"{TRADING_DATE}T14:50:10+08:00",
                "--json",
            ]
        )
        == 0
    )
    decision = json.loads(capsys.readouterr().out)

    assert decision["status"] == "no_action_portfolio"
    assert decision["signals_before_portfolio"] == 1
    assert decision["signals"] == []
    assert decision["has_signal"] is False
    assert decision["portfolio_coordination"]["allocations"][0]["strategy_id"] == "trend:v1"
    rejection = decision["portfolio_coordination"]["rejections"]
    assert len(rejection) == 1
    assert rejection[0]["code"] == "600001"
    assert rejection[0]["reason"] == "duplicate_security"
    assert rejection[0]["strategy_id"] == decision["strategy_id"]
    assert signal_ledger.read_events(str(ledger)) == []


def test_manual_reconciliation_rejects_self_signed_persisted_evidence(
    tmp_path,
    monkeypatch,
    capsys,
):
    ledger = tmp_path / "signal_ledger.jsonl"
    monkeypatch.setattr(signal_ledger, "LEDGER_FILE", str(ledger))
    cfg = config()
    cfg["safety"]["manual_pilot_reconciliation_enabled"] = True
    config_path = _write(tmp_path / "config.json", cfg)
    strategy_id = "tail_close:mainline_continuation_v1"
    oos = _artifact(
        {
            "schema": "tail_close_validation_gate_v1",
            "strategy_id": strategy_id,
            "config_hash": strategy_family_config_hash(cfg, strategy_id),
            "precommit_id": "precommit-1",
            "precommit_record_sha256": "1" * 64,
            "reveal_record_sha256": "2" * 64,
            "dataset_sha256": "3" * 64,
            "status": "passed",
            "allowed_next_state": "shadow",
        }
    )
    shadow = _artifact(
        {
            "schema": "tail_close_shadow_readiness_v1",
            "strategy_id": strategy_id,
            "config_hash": oos["config_hash"],
            "oos_artifact_hash": oos["artifact_hash"],
            "status": "passed",
            "allowed_next_state": "eligible_for_manual_pilot",
        }
    )
    approval = _artifact(
        {
            "schema": "tail_close_human_approval_v1",
            "strategy_id": strategy_id,
            "approval_id": "approval-persisted-1",
            "approved_at": f"{TRADING_DATE}T16:00:00+08:00",
            "explicit_human_approval": True,
            "oos_artifact_hash": oos["artifact_hash"],
            "shadow_artifact_hash": shadow["artifact_hash"],
        }
    )
    oos_path = _write(tmp_path / "oos.json", oos)
    shadow_path = _write(tmp_path / "shadow.json", shadow)
    approval_path = _write(tmp_path / "approval.json", approval)
    registry_path = _write(tmp_path / "oos-registry.jsonl", {})
    dataset_path = _write(tmp_path / "oos-dataset.json", {"outcomes": []})
    evaluation_path = _write(
        tmp_path / "oos-evaluation.json",
        {"variant_returns": {"mainline": []}},
    )
    observations_path = _write(
        tmp_path / "shadow-observations.json",
        {"observations": []},
    )
    evidence_path = tmp_path / "broker-statement.txt"
    evidence_path.write_text("external manual fill evidence", encoding="utf-8")
    fill = {
        "schema": "tail_close_simulated_fill_v1",
        "strategy_id": strategy_id,
        "signal_id": "tail-manual-1",
        "trading_date": TRADING_DATE,
        "status": "FULL_FILL",
        "filled_quantity": 1_000,
        "fill_price": 10.31,
        "provenance": {
            "decision_mode": "replay",
            "snapshot_id": "snapshot-manual-1",
            "snapshot_hash": "a" * 64,
            "config_hash": canonical_hash(cfg),
            "code_version": "test-commit",
        },
        "simulation": True,
        "research_only": True,
        "live_weight": 0,
    }
    fill["fill_hash"] = canonical_hash(fill)
    manual_execution = {
        "evidence_path": str(evidence_path),
        "actual_filled_quantity": 1_000,
        "actual_fill_price": 10.31,
        "external_broker_evidence_confirmed": True,
    }
    payload = {
        "oos_result_path": oos_path,
        "shadow_result_path": shadow_path,
        "human_approval_path": approval_path,
        "precommit_registry_path": registry_path,
        "oos_dataset_path": dataset_path,
        "oos_evaluation_input_path": evaluation_path,
        "shadow_observations_path": observations_path,
        "simulation_fill": fill,
        "manual_execution": manual_execution,
    }
    input_path = _write(tmp_path / "manual-input.json", payload)
    args = [
        "manual-reconcile",
        "--config",
        config_path,
        "--input",
        input_path,
        "--output",
        str(tmp_path / "manual-output.json"),
        "--json",
    ]

    assert tail_close_signal.main(args) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["reason"] == "oos_registry_invalid"
    assert signal_ledger.read_events(str(ledger)) == []


def test_missing_input_fails_closed_without_signal(capsys):
    exit_code = tail_close_signal.main(
        ["decision", "--input", "/definitely/missing/tail-close.json", "--json"]
    )
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert result["status"] == "blocked"
    assert result["signals"] == []
    assert result["broker_call_count"] == 0


def test_retry_rejects_mutated_canonical_decision(tmp_path, capsys):
    prepare_input = _write(tmp_path / "prepare.json", bundle(prepare=True))
    prepared = tmp_path / "prepared.json"
    assert (
        tail_close_signal.main(
            [
                "prepare",
                "--input",
                prepare_input,
                "--output",
                str(prepared),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    decision_input = _write(tmp_path / "decision.json", bundle())
    output = tmp_path / "decision-output.json"
    args = [
        "decision",
        "--input",
        decision_input,
        "--output",
        str(output),
        "--prepared",
        str(prepared),
        "--emitted-at",
        f"{TRADING_DATE}T14:50:10+08:00",
        "--json",
    ]
    assert tail_close_signal.main(args) == 0
    capsys.readouterr()
    stored = json.loads(output.read_text(encoding="utf-8"))
    stored["signals"] = []
    output.write_text(json.dumps(stored), encoding="utf-8")

    assert tail_close_signal.main(args) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "canonical_decision_hash_mismatch"


def test_retry_cannot_reuse_decision_after_prepare_artifact_disappears(
    tmp_path,
    capsys,
):
    prepare_input = _write(tmp_path / "prepare.json", bundle(prepare=True))
    decision_input = _write(tmp_path / "decision.json", bundle())
    prepared = tmp_path / "prepared.json"
    output = tmp_path / "decision-output.json"
    assert tail_close_signal.main(
        [
            "prepare",
            "--input",
            prepare_input,
            "--output",
            str(prepared),
            "--json",
        ]
    ) == 0
    capsys.readouterr()
    args = [
        "decision",
        "--input",
        decision_input,
        "--prepared",
        str(prepared),
        "--output",
        str(output),
        "--emitted-at",
        f"{TRADING_DATE}T14:50:10+08:00",
        "--json",
    ]
    assert tail_close_signal.main(args) == 0
    capsys.readouterr()

    prepared.unlink()
    assert tail_close_signal.main(args) == 2
    blocked = json.loads(capsys.readouterr().out)

    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "prepared_state_missing"


def test_after_hours_job_stays_not_ready_until_independent_capability_passes(capsys):
    assert tail_close_signal.main(["after-hours-shadow", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "not_ready"
    assert result["strategy_id"] == "tail_close:after_hours_fixed_v1"
    assert result["broker_call_count"] == 0


def test_after_hours_signal_is_frozen_before_outcomes_are_reconciled(
    tmp_path,
    capsys,
):
    cfg = config()
    sibling = cfg["strategies"]["tail_close:after_hours_fixed_v1"]
    sibling["enabled"] = True
    sibling["readiness"] = "ready"
    config_path = _write(tmp_path / "config.json", cfg)
    signal_input = _write(
        tmp_path / "after-hours-signal.json",
        {
            "signal": {
                "strategy_id": "tail_close:after_hours_fixed_v1",
                "trading_date": TRADING_DATE,
                "close_price": 10.0,
                "requested_notional": 100_000,
                "queue_observable": True,
            }
        },
    )
    frozen_path = tmp_path / "frozen.json"

    assert (
        tail_close_signal.main(
            [
                "after-hours-shadow",
                "--config",
                config_path,
                "--input",
                signal_input,
                "--output",
                str(frozen_path),
                "--json",
            ]
        )
        == 0
    )
    frozen_output = json.loads(capsys.readouterr().out)
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    assert frozen_output["status"] == "frozen"
    assert "observations" not in frozen
    assert "filled_quantity" not in frozen

    reconcile_input = _write(
        tmp_path / "after-hours-reconcile.json",
        {
            "frozen_signal": frozen,
            "observations": [
                {
                    "event_time": f"{TRADING_DATE}T15:06:00+08:00",
                    "available_time": f"{TRADING_DATE}T15:06:01+08:00",
                    "incremental_matched_sell_volume": 20_000,
                }
            ],
        },
    )
    assert (
        tail_close_signal.main(
            [
                "after-hours-reconcile",
                "--config",
                config_path,
                "--input",
                reconcile_input,
                "--output",
                str(tmp_path / "after-hours-fill.json"),
                "--json",
            ]
        )
        == 0
    )
    reconciled = json.loads(capsys.readouterr().out)
    assert reconciled["status"] == "FULL_FILL"
    assert reconciled["filled_quantity"] == 10_000
    assert reconciled["broker_called"] is False
