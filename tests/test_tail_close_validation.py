import json
import subprocess
import sys
from pathlib import Path

import pytest

from a_share_rules import add_trading_days
from tail_close_strategy import (
    AFTER_HOURS_STRATEGY_ID,
    PRIMARY_STRATEGY_ID,
    canonical_hash,
    evaluate_kill_switch,
)
from tail_close_test_support import config
from tail_close_validation import (
    TailCloseValidationError,
    evaluate_manual_pilot_eligibility,
    evaluate_oos_family,
    evaluate_shadow_readiness,
    strategy_family_config_hash,
)
from validation_program import load_validation_thresholds


ROOT = Path(__file__).resolve().parents[1]


def _thresholds():
    return load_validation_thresholds(ROOT / "config" / "validation_thresholds.json")


def _outcomes(count=20, strategy_id=PRIMARY_STRATEGY_ID):
    return [
        {
            "trade_id": f"trade-{index}",
            "signal_id": f"signal-{index}",
            "strategy_id": strategy_id,
            "code": f"{600000 + index:06d}",
            "trading_date": f"2026-06-{index % 20 + 1:02d}",
            "regime": f"R{index % 4}",
            "net_return": 0.012 if index % 4 else -0.004,
            "incremental_net_return": 0.004,
            "status": "exited",
            "observation_complete": True,
            "right_censored": False,
            "days_blocked": 0,
            "research_only": True,
            "broker_call_count": 0,
            "automatic_order_count": 0,
            "live_weight": 0,
        }
        for index in range(count)
    ]


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _registered_evidence(
    tmp_path,
    cfg,
    outcomes,
    variant_returns,
    *,
    strategy_id=PRIMARY_STRATEGY_ID,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    rules = repo / "rules.json"
    dataset = repo / "dataset.json"
    thresholds = repo / "thresholds.json"
    rules.write_text(json.dumps(cfg, sort_keys=True), encoding="utf-8")
    dataset.write_text(
        json.dumps({"outcomes": outcomes}, sort_keys=True),
        encoding="utf-8",
    )
    thresholds.write_text(json.dumps(_thresholds(), sort_keys=True), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")

    registry = tmp_path / "oos-registry.jsonl"
    split = {
        "method": "walk_forward",
        "strategy_id": strategy_id,
        "config_hash": strategy_family_config_hash(cfg, strategy_id),
        "primary_variant": "mainline",
        "seed": 7,
        "purge_and_embargo": True,
        "multiple_testing_correction": True,
    }
    variants = sorted(variant_returns)
    folds = ["fold-0"]
    common_path = ROOT / "skills" / "common"
    create_script = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from validation_program import OOSRegistry
OOSRegistry(sys.argv[2], sys.argv[3], invocation_id="precommit-run").create_precommit(
    sys.argv[4], sys.argv[5], split=json.loads(sys.argv[6]),
    variants=json.loads(sys.argv[7]), fold_ids=json.loads(sys.argv[8]),
    thresholds_path=sys.argv[9],
)
"""
    subprocess.run(
        [
            sys.executable,
            "-c",
            create_script,
            str(common_path),
            str(registry),
            str(repo),
            str(rules),
            str(dataset),
            json.dumps(split),
            json.dumps(variants),
            json.dumps(folds),
            str(thresholds),
        ],
        check=True,
    )
    precommit = next(
        json.loads(line)
        for line in registry.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("record_type") == "precommit"
    )
    reveal_payload = {
        variant: {
            "status": "passed",
            "returns_hash": canonical_hash(list(map(float, values))),
            "sample_count": len(values),
        }
        for variant, values in variant_returns.items()
    }
    reveal_script = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from validation_program import OOSRegistry
OOSRegistry(sys.argv[2], sys.argv[3], invocation_id="reveal-run").register_result(
    sys.argv[4], sys.argv[5], sys.argv[6],
    variant_results=json.loads(sys.argv[7]),
    fold_results={"fold-0": {"status": "passed"}},
    thresholds_path=sys.argv[8],
)
"""
    subprocess.run(
        [
            sys.executable,
            "-c",
            reveal_script,
            str(common_path),
            str(registry),
            str(repo),
            precommit["precommit_id"],
            str(rules),
            str(dataset),
            json.dumps(reveal_payload),
            str(thresholds),
        ],
        check=True,
    )
    reveal = next(
        json.loads(line)
        for line in registry.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("record_type") == "result"
    )
    return {
        "precommit_registry_path": registry,
        "precommit_id": precommit["precommit_id"],
        "reveal_record_sha256": reveal["record_sha256"],
        "dataset_path": dataset,
        "validation_thresholds_path": thresholds,
    }


def _artifact(payload):
    result = dict(payload)
    result["artifact_hash"] = canonical_hash(result)
    return result


def _valid_oos_gate(cfg, strategy_id=PRIMARY_STRATEGY_ID):
    return _artifact(
        {
            "schema": "tail_close_validation_gate_v1",
            "strategy_id": strategy_id,
            "status": "passed",
            "allowed_next_state": "shadow",
            "config_hash": strategy_family_config_hash(cfg, strategy_id),
        }
    )


def test_oos_gate_cannot_pass_before_preregistered_sample_floor(tmp_path):
    cfg = config()
    outcomes = _outcomes()
    returns = [item["net_return"] for item in outcomes]
    variant_returns = {
        "mainline": returns,
        "control": [value - 0.003 for value in returns],
    }
    evidence = _registered_evidence(tmp_path, cfg, outcomes, variant_returns)

    result = evaluate_oos_family(
        strategy_id=PRIMARY_STRATEGY_ID,
        outcomes=outcomes,
        variant_returns=variant_returns,
        **evidence,
        strategy_config=cfg,
        validation_thresholds=_thresholds(),
    )

    assert result["status"] == "failed"
    assert result["allowed_next_state"] == "research_only"
    assert result["checks"]["raw_sample_sufficient"] is False
    assert result["raw_filled_trades"] == 20
    assert result["live_weight"] == 0


def test_after_hours_family_cannot_borrow_primary_precommit(tmp_path):
    cfg = config()
    outcomes = _outcomes(strategy_id=AFTER_HOURS_STRATEGY_ID)
    variant_returns = {
        "mainline": [item["net_return"] for item in outcomes],
        "control": [0.005] * len(outcomes),
    }
    evidence = _registered_evidence(
        tmp_path,
        cfg,
        outcomes,
        variant_returns,
        strategy_id=PRIMARY_STRATEGY_ID,
    )

    with pytest.raises(TailCloseValidationError, match="precommit_strategy_mismatch"):
        evaluate_oos_family(
            strategy_id=AFTER_HOURS_STRATEGY_ID,
            outcomes=outcomes,
            variant_returns=variant_returns,
            **evidence,
            strategy_config=cfg,
            validation_thresholds=_thresholds(),
        )


def test_oos_returns_must_come_from_same_isolated_outcomes(tmp_path):
    cfg = config()
    outcomes = _outcomes()
    for item in outcomes:
        item["net_return"] = -0.05
        item["broker_call_count"] = 9
        item["live_weight"] = 1
    variant_returns = {
        "mainline": [0.05] * len(outcomes),
        "control": [0.01] * len(outcomes),
    }
    evidence = _registered_evidence(tmp_path, cfg, outcomes, variant_returns)

    with pytest.raises(
        TailCloseValidationError,
        match="primary_returns_not_derived_from_outcomes",
    ):
        evaluate_oos_family(
            strategy_id=PRIMARY_STRATEGY_ID,
            outcomes=outcomes,
            variant_returns=variant_returns,
            **evidence,
            strategy_config=cfg,
            validation_thresholds=_thresholds(),
        )


def test_pending_and_censored_outcomes_never_advance_oos_sample_gate(tmp_path):
    cfg = config()
    cfg["validation"]["oos"]["minimum_simulated_filled_samples"] = 22
    outcomes = _outcomes(22)
    outcomes[20].update(
        {
            "status": "blocked_pending",
            "observation_complete": False,
            "net_return": 9.0,
            "incremental_net_return": 9.0,
        }
    )
    outcomes[21].update(
        {
            "status": "right_censored",
            "observation_complete": True,
            "right_censored": True,
            "net_return": 9.0,
            "incremental_net_return": 9.0,
        }
    )
    eligible_returns = [item["net_return"] for item in outcomes[:20]]
    variant_returns = {
        "mainline": eligible_returns,
        "control": [value - 0.003 for value in eligible_returns],
    }
    evidence = _registered_evidence(tmp_path, cfg, outcomes, variant_returns)

    result = evaluate_oos_family(
        strategy_id=PRIMARY_STRATEGY_ID,
        outcomes=outcomes,
        variant_returns=variant_returns,
        **evidence,
        strategy_config=cfg,
        validation_thresholds=_thresholds(),
    )

    assert result["status"] == "failed"
    assert result["raw_filled_trades"] == 20
    assert result["submitted_outcomes"] == 22
    assert result["pending_outcomes"] == 1
    assert result["right_censored_count"] == 1
    assert result["checks"]["outcome_accounting_conserved"] is True
    assert result["checks"]["all_outcomes_complete"] is False
    assert result["checks"]["raw_sample_sufficient"] is False
    assert result["incremental_observations"] == 20


def test_all_pending_outcomes_produce_failed_artifact_not_statistics_error(tmp_path):
    cfg = config()
    outcomes = _outcomes(1)
    outcomes[0].update(
        {
            "status": "blocked_pending",
            "observation_complete": False,
        }
    )
    variant_returns = {"mainline": [], "control": []}
    evidence = _registered_evidence(tmp_path, cfg, outcomes, variant_returns)

    result = evaluate_oos_family(
        strategy_id=PRIMARY_STRATEGY_ID,
        outcomes=outcomes,
        variant_returns=variant_returns,
        **evidence,
        strategy_config=cfg,
        validation_thresholds=_thresholds(),
    )

    assert result["status"] == "failed"
    assert result["raw_filled_trades"] == 0
    assert result["pending_outcomes"] == 1
    assert result["statistics"]["status"] == "not_evaluated"
    assert result["statistics"]["reasons"] == ["completed_sample_insufficient"]


def test_oos_rejects_dataset_changed_after_registered_reveal(tmp_path):
    cfg = config()
    outcomes = _outcomes()
    variant_returns = {
        "mainline": [item["net_return"] for item in outcomes],
        "control": [item["net_return"] - 0.003 for item in outcomes],
    }
    evidence = _registered_evidence(tmp_path, cfg, outcomes, variant_returns)
    Path(evidence["dataset_path"]).write_text('{"outcomes":[]}', encoding="utf-8")

    with pytest.raises(TailCloseValidationError, match="oos_dataset_hash_mismatch"):
        evaluate_oos_family(
            strategy_id=PRIMARY_STRATEGY_ID,
            outcomes=outcomes,
            variant_returns=variant_returns,
            **evidence,
            strategy_config=cfg,
            validation_thresholds=_thresholds(),
        )


def test_oos_rejects_thresholds_changed_after_precommit(tmp_path):
    cfg = config()
    outcomes = _outcomes()
    variant_returns = {
        "mainline": [item["net_return"] for item in outcomes],
        "control": [item["net_return"] - 0.003 for item in outcomes],
    }
    evidence = _registered_evidence(tmp_path, cfg, outcomes, variant_returns)
    thresholds_path = Path(evidence["validation_thresholds_path"])
    loosened = _thresholds()
    loosened["statistics"]["minimum_observations"] = 1
    thresholds_path.write_text(
        json.dumps(loosened, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(
        TailCloseValidationError,
        match="validation_thresholds_hash_mismatch",
    ):
        evaluate_oos_family(
            strategy_id=PRIMARY_STRATEGY_ID,
            outcomes=outcomes,
            variant_returns=variant_returns,
            **evidence,
            strategy_config=cfg,
            validation_thresholds=loosened,
        )


def test_oos_rejects_unregistered_reveal_identity(tmp_path):
    cfg = config()
    outcomes = _outcomes()
    variant_returns = {
        "mainline": [item["net_return"] for item in outcomes],
        "control": [item["net_return"] - 0.003 for item in outcomes],
    }
    evidence = _registered_evidence(tmp_path, cfg, outcomes, variant_returns)
    evidence["reveal_record_sha256"] = "0" * 64

    with pytest.raises(TailCloseValidationError, match="reveal_record_missing"):
        evaluate_oos_family(
            strategy_id=PRIMARY_STRATEGY_ID,
            outcomes=outcomes,
            variant_returns=variant_returns,
            **evidence,
            strategy_config=cfg,
            validation_thresholds=_thresholds(),
        )


def test_oos_rejects_returns_not_committed_by_reveal(tmp_path):
    cfg = config()
    outcomes = _outcomes()
    registered_returns = {
        "mainline": [item["net_return"] for item in outcomes],
        "control": [item["net_return"] - 0.003 for item in outcomes],
    }
    evidence = _registered_evidence(tmp_path, cfg, outcomes, registered_returns)
    substituted_returns = {
        "mainline": registered_returns["mainline"],
        "control": [0.99] * len(outcomes),
    }

    with pytest.raises(TailCloseValidationError, match="returns_not_bound_to_reveal"):
        evaluate_oos_family(
            strategy_id=PRIMARY_STRATEGY_ID,
            outcomes=outcomes,
            variant_returns=substituted_returns,
            **evidence,
            strategy_config=cfg,
            validation_thresholds=_thresholds(),
        )


def test_duplicate_trade_identity_cannot_inflate_oos_samples(tmp_path):
    cfg = config()
    cfg["validation"]["oos"]["minimum_simulated_filled_samples"] = 21
    outcomes = _outcomes(21)
    outcomes[-1]["trade_id"] = outcomes[0]["trade_id"]
    eligible_returns = [item["net_return"] for item in outcomes[:20]]
    variant_returns = {
        "mainline": eligible_returns,
        "control": [value - 0.003 for value in eligible_returns],
    }
    evidence = _registered_evidence(tmp_path, cfg, outcomes, variant_returns)

    result = evaluate_oos_family(
        strategy_id=PRIMARY_STRATEGY_ID,
        outcomes=outcomes,
        variant_returns=variant_returns,
        **evidence,
        strategy_config=cfg,
        validation_thresholds=_thresholds(),
    )

    assert result["status"] == "failed"
    assert result["raw_filled_trades"] == 20
    assert result["invalid_outcomes"] == 1
    assert result["checks"]["outcome_accounting_conserved"] is True
    assert result["checks"]["all_outcomes_complete"] is False


def test_shadow_requires_sixty_distinct_real_observation_days():
    cfg = config()
    observations = [
        {
            "trading_date": add_trading_days("2026-01-05", index).isoformat(),
            "strategy_id": PRIMARY_STRATEGY_ID,
            "observation_mode": "live_shadow",
            "signal_frozen_before_outcome": True,
            "live_effect": "none",
            "point_in_time_valid": True,
            "major_incident": False,
            "simulation_error": 0.01,
            "broker_call_count": 0,
            "automatic_order_count": 0,
            "live_weight": 0,
            "ledger_audit_mismatches": 0,
            "pit_sla_incidents": 0,
        }
        for index in range(60)
    ]

    not_ready = evaluate_shadow_readiness(
        strategy_id=PRIMARY_STRATEGY_ID,
        observations=observations[:-1],
        oos_result=_valid_oos_gate(cfg),
        strategy_config=cfg,
        validation_thresholds=_thresholds(),
    )
    ready = evaluate_shadow_readiness(
        strategy_id=PRIMARY_STRATEGY_ID,
        observations=observations,
        oos_result=_valid_oos_gate(cfg),
        strategy_config=cfg,
        validation_thresholds=_thresholds(),
    )

    assert not_ready["status"] == "not_ready"
    assert ready["status"] == "passed"
    assert ready["allowed_next_state"] == "eligible_for_manual_pilot"
    assert ready["broker_call_count"] == 0


def test_shadow_cannot_borrow_oos_or_observations_from_sibling():
    observation = {
        "trading_date": "2026-01-05",
        "strategy_id": AFTER_HOURS_STRATEGY_ID,
        "observation_mode": "live_shadow",
        "signal_frozen_before_outcome": True,
        "live_effect": "none",
        "point_in_time_valid": True,
        "major_incident": False,
        "simulation_error": 0,
        "broker_call_count": 0,
        "automatic_order_count": 0,
        "live_weight": 0,
        "ledger_audit_mismatches": 0,
        "pit_sla_incidents": 0,
    }
    result = evaluate_shadow_readiness(
        strategy_id=PRIMARY_STRATEGY_ID,
        observations=[observation],
        oos_result=_valid_oos_gate(config(), AFTER_HOURS_STRATEGY_ID),
        strategy_config=config(),
        validation_thresholds=_thresholds(),
    )

    assert result["status"] == "not_ready"
    assert result["checks"]["matching_oos_passed"] is False
    assert result["checks"]["observation_family_isolated"] is False


def test_manual_pilot_needs_separate_human_approval_after_both_gates():
    oos = _valid_oos_gate(config())
    shadow = _artifact(
        {
            "schema": "tail_close_shadow_readiness_v1",
            "strategy_id": PRIMARY_STRATEGY_ID,
            "config_hash": oos["config_hash"],
            "oos_artifact_hash": oos["artifact_hash"],
            "status": "passed",
            "allowed_next_state": "eligible_for_manual_pilot",
        }
    )

    blocked = evaluate_manual_pilot_eligibility(
        strategy_id=PRIMARY_STRATEGY_ID,
        oos_result=oos,
        shadow_result=shadow,
        explicit_human_approval=False,
    )
    eligible = evaluate_manual_pilot_eligibility(
        strategy_id=PRIMARY_STRATEGY_ID,
        oos_result=oos,
        shadow_result=shadow,
        explicit_human_approval=True,
    )

    assert blocked["status"] == "not_eligible"
    assert eligible["status"] == "eligible_for_manual_pilot"
    assert eligible["system_ordering"] == "forbidden"
    assert eligible["broker_call_count"] == 0

    mismatched_oos = _valid_oos_gate(config())
    mismatched_oos = _artifact(
        {
            key: value
            for key, value in mismatched_oos.items()
            if key != "artifact_hash"
        }
        | {"precommit_id": "different-valid-oos"}
    )
    mismatch = evaluate_manual_pilot_eligibility(
        strategy_id=PRIMARY_STRATEGY_ID,
        oos_result=mismatched_oos,
        shadow_result=shadow,
        explicit_human_approval=True,
    )
    assert mismatch["status"] == "not_eligible"
    assert mismatch["checks"]["matching_oos_family"] is True
    assert mismatch["checks"]["matching_shadow_family"] is False


def test_kill_switch_fails_closed_on_integrity_or_incremental_edge_loss():
    result = evaluate_kill_switch(
        {
            "strategy_id": PRIMARY_STRATEGY_ID,
            "pit_violations": 1,
            "broker_call_count": 0,
            "live_weight": 0,
            "ledger_audit_mismatches": 1,
            "fill_rate_error": 0.03,
            "incremental_net_expectancy": -0.001,
        },
        config(),
    )

    assert result["blocked"] is True
    assert set(result["reasons"]) == {
        "pit_violation",
        "ledger_audit_mismatch",
        "fill_model_drift",
        "incremental_edge_lost",
    }
    assert result["required_state"] == "research_only"
    assert result["scope"] == "strategy_lane"
    assert result["affected_strategy_id"] == PRIMARY_STRATEGY_ID


def test_global_risk_kill_switch_is_the_only_cross_lane_scope():
    result = evaluate_kill_switch(
        {
            "strategy_id": AFTER_HOURS_STRATEGY_ID,
            "global_risk_incidents": 1,
            "broker_call_count": 0,
            "live_weight": 0,
        },
        config(),
    )

    assert result["blocked"] is True
    assert result["scope"] == "all_strategies"
    assert result["affected_strategy_id"] == "*"
