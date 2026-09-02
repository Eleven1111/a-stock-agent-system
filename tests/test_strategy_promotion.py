import hashlib
import importlib.util
import json
from datetime import datetime, timezone

import pytest

import strategy_registry as sr
from scripts import strategy_promotion_runner as promotion_runner


RESEARCH_GATE = importlib.util.spec_from_file_location(
    "issue324_research_gate",
    __file__.replace("tests/test_strategy_promotion.py", "skills/chanlun-backtest/scripts/research_gate.py"),
)
research_gate = importlib.util.module_from_spec(RESEARCH_GATE)
RESEARCH_GATE.loader.exec_module(research_gate)


def _canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _thresholds(tmp_path, *, version="v1", shadow_days=2):
    path = tmp_path / f"thresholds-{version}.json"
    payload = {
        "schema_version": version,
        "effective_date": "2026-07-10",
        "empirical": {
            "minimum_real_trading_days": 60,
            "minimum_trade_effective_samples": 2,
            "minimum_stock_effective_samples": 2,
            "minimum_regime_effective_samples": 2,
        },
        "statistics": {
            "minimum_observations": 20,
            "block_length": 4,
            "bootstrap_resamples": 100,
            "fdr_alpha": 0.05,
            "hac_lags": 2,
            "pbo_partitions": 4,
            "maximum_pbo": 0.5,
            "minimum_deflated_sharpe_probability": 0.95,
        },
        "shadow": {
            "minimum_trading_days": shadow_days,
            "maximum_simulation_error": 0.02,
            "auto_demotion_error": 0.05,
            "maximum_manual_pilot_weight": 0.1,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, payload


def _precommit(path, *, identifier="precommit-v1"):
    record = {
        "record_type": "precommit",
        "schema_version": "oos-precommit-v1",
        "precommit_id": identifier,
        "clean_tree": True,
        "ancestor_commit": "a" * 40,
        "variants": ["base"],
        "fold_ids": ["fold-0"],
        "thresholds_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "previous_record_sha256": None,
    }
    record["record_sha256"] = _canonical_hash(record)
    return record


def _bound_artifact(**core):
    return {**core, "artifact_sha256": _canonical_hash(core)}


def _shadow(precommit, threshold_hash, *, status="eligible_for_manual_pilot", error=0.01):
    return _bound_artifact(
        schema_version="shadow-window-v1",
        computed_by="validation_program-v1",
        strategy_id="strategy-a",
        precommit_id=precommit["precommit_id"],
        thresholds_sha256=threshold_hash,
        thresholds={"schema_version": "fixture"},
        status=status,
        observed_trading_days=60,
        simulation_error=error,
        reason="shadow_thresholds_met" if status != "research_only" else "auto_demoted",
        recorded_at="2026-07-10T00:00:00+00:00",
    )


def _empirical_gate(*, days=60, trade=2, stock=2, regime=2, status="passed"):
    reasons = [] if status == "passed" else ["statistics_failed"]
    core = {
        "schema_version": "empirical-validation-gate-v1",
        "computed_by": "validation_program-v1",
        "real_trading_days": days,
        "effective_samples": {
            "status": "evaluated", "trade": float(trade), "stock": float(stock),
            "regime": float(regime), "input_sha256": "b" * 64,
        },
        "statistics_status": "passed" if status == "passed" else "failed",
        "shadow_status": "eligible_for_manual_pilot",
        "broker_status": "reconciled",
        "reasons": reasons,
        "status": status,
        "production_release": "eligible_for_review" if status == "passed" else "blocked",
    }
    return _bound_artifact(**core)


def _register_legacy_gate(registry, verified_gate_factory):
    sr.register_gate_result(
        "strategy-a", verified_gate_factory("strategy-a"), registry_file=str(registry)
    )


def test_promotion_has_exact_transitions_approval_and_bounded_weight(
    tmp_path, verified_gate_factory
):
    registry = tmp_path / "strategy-registry.json"
    _register_legacy_gate(registry, verified_gate_factory)
    thresholds_path, _ = _thresholds(tmp_path)
    precommit = _precommit(thresholds_path)
    threshold_hash = precommit["thresholds_sha256"]

    shadow = sr.start_shadow(
        "strategy-a", precommit=precommit, thresholds_path=str(thresholds_path),
        registry_file=str(registry),
    )
    assert shadow["promotion"]["state"] == "shadow"
    assert sr.start_shadow(
        "strategy-a", precommit=precommit, thresholds_path=str(thresholds_path),
        registry_file=str(registry),
    )["promotion"]["state"] == "shadow"
    assert sr.live_weight("strategy-a", str(registry)) == 0.0
    with pytest.raises(ValueError, match="invalid_promotion_transition"):
        sr.promote_strategy(
            "strategy-a", "manual_pilot", registry_file=str(registry)
        )
    with pytest.raises(ValueError, match="promotion_evidence_missing"):
        sr.promote_strategy(
            "strategy-a", "eligible_for_manual_pilot", registry_file=str(registry)
        )

    shadow_artifact = _shadow(precommit, threshold_hash)
    gate = _empirical_gate()
    eligible = sr.promote_strategy(
        "strategy-a", "eligible_for_manual_pilot", empirical_gate=gate,
        shadow_record=shadow_artifact, registry_file=str(registry),
    )
    assert eligible["promotion"]["state"] == "eligible_for_manual_pilot"
    checked = sr.apply_promotion_safety_check(
        "strategy-a", thresholds_path=str(thresholds_path),
        shadow_record=shadow_artifact,
        broker_report=_bound_artifact(
            schema_version="broker-reconciliation-v1", computed_by="validation_program-v1",
            status="reconciled"
        ),
        registry_file=str(registry),
    )
    assert checked["promotion"]["state"] == "eligible_for_manual_pilot"
    with pytest.raises(ValueError, match="manual_approval_required"):
        sr.promote_strategy(
            "strategy-a", "manual_pilot", requested_weight=0.05,
            registry_file=str(registry),
        )
    approval = {
        "approved": True,
        "strategy_id": "strategy-a",
        "approver": "owner",
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    with pytest.raises(ValueError, match="pilot_weight_exceeded"):
        sr.promote_strategy(
            "strategy-a", "manual_pilot", requested_weight=0.11,
            human_approval=approval, registry_file=str(registry),
        )
    pilot = sr.promote_strategy(
        "strategy-a", "manual_pilot", requested_weight=0.1,
        human_approval=approval, registry_file=str(registry),
    )
    assert pilot["promotion"]["state"] == "manual_pilot"
    assert sr.live_weight("strategy-a", str(registry)) == pytest.approx(0.1)
    live = sr.promote_strategy(
        "strategy-a", "live", empirical_gate=gate,
        shadow_record=shadow_artifact, registry_file=str(registry),
    )
    assert live["promotion"]["state"] == "live"
    assert sr.live_weight("strategy-a", str(registry)) == 1.0


def test_promotion_requires_60_days_samples_statistics_and_broker(
    tmp_path, verified_gate_factory
):
    registry = tmp_path / "strategy-registry.json"
    assert sr.promotion_state("missing", str(registry))["state"] == "research_only"
    _register_legacy_gate(registry, verified_gate_factory)
    thresholds_path, _ = _thresholds(tmp_path)
    precommit = _precommit(thresholds_path)
    sr.start_shadow(
        "strategy-a", precommit=precommit, thresholds_path=str(thresholds_path),
        registry_file=str(registry),
    )
    shadow = _shadow(precommit, precommit["thresholds_sha256"])
    forged_precommit = dict(precommit)
    forged_precommit.pop("record_sha256")
    with pytest.raises(ValueError, match="threshold_precommit_mismatch"):
        sr.start_shadow(
            "strategy-b", precommit=forged_precommit,
            thresholds_path=str(thresholds_path), registry_file=str(registry),
        )
    attested_gate = _empirical_gate()
    attested_gate.pop("computed_by")
    attested_gate.pop("artifact_sha256")
    attested_gate = _bound_artifact(**attested_gate)
    with pytest.raises(ValueError, match="promotion_evidence_insufficient"):
        sr.promote_strategy(
            "strategy-a", "eligible_for_manual_pilot", empirical_gate=attested_gate,
            shadow_record=shadow, registry_file=str(registry),
        )
    for gate in (
        _empirical_gate(days=59),
        _empirical_gate(stock=1),
        _empirical_gate(status="failed"),
    ):
        with pytest.raises(ValueError, match="promotion_evidence_insufficient"):
            sr.promote_strategy(
                "strategy-a", "eligible_for_manual_pilot", empirical_gate=gate,
                shadow_record=shadow, registry_file=str(registry),
            )
    assert sr.promotion_state("strategy-a", str(registry))["state"] == "shadow"


def test_safety_breach_demotes_and_new_threshold_precommit_resets_shadow(
    tmp_path, verified_gate_factory
):
    registry = tmp_path / "strategy-registry.json"
    _register_legacy_gate(registry, verified_gate_factory)
    thresholds_path, _ = _thresholds(tmp_path)
    precommit = _precommit(thresholds_path)
    sr.start_shadow(
        "strategy-a", precommit=precommit, thresholds_path=str(thresholds_path),
        registry_file=str(registry),
    )
    shadow = _shadow(precommit, precommit["thresholds_sha256"])
    sr.promote_strategy(
        "strategy-a", "eligible_for_manual_pilot", empirical_gate=_empirical_gate(),
        shadow_record=shadow, registry_file=str(registry),
    )
    demoted = sr.apply_promotion_safety_check(
        "strategy-a", thresholds_path=str(thresholds_path),
        shadow_record=_shadow(
            precommit, precommit["thresholds_sha256"],
            status="eligible_for_manual_pilot", error=0.05
        ),
        broker_report=_bound_artifact(
            schema_version="broker-reconciliation-v1", computed_by="validation_program-v1",
            status="reconciled"
        ),
        registry_file=str(registry),
    )
    assert demoted["promotion"]["state"] == "research_only"
    assert demoted["promotion"]["reason"] == "auto_demoted"
    assert sr.live_weight("strategy-a", str(registry)) == 0.0

    new_path, _ = _thresholds(tmp_path, version="v2", shadow_days=3)
    new_precommit = _precommit(new_path, identifier="precommit-v2")
    reset = sr.start_shadow(
        "strategy-a", precommit=new_precommit, thresholds_path=str(new_path),
        registry_file=str(registry),
    )
    assert reset["promotion"]["state"] == "shadow"
    assert reset["promotion"]["reason"] == "shadow_window_reset"
    assert reset["promotion"]["observed_trading_days"] == 0

    new_shadow = _shadow(new_precommit, new_precommit["thresholds_sha256"])
    sr.promote_strategy(
        "strategy-a", "eligible_for_manual_pilot", empirical_gate=_empirical_gate(),
        shadow_record=new_shadow, registry_file=str(registry),
    )
    mismatch = sr.apply_promotion_safety_check(
        "strategy-a", thresholds_path=str(new_path), shadow_record=new_shadow,
        broker_report=_bound_artifact(
            schema_version="broker-reconciliation-v1", computed_by="validation_program-v1",
            status="mismatch"
        ),
        registry_file=str(registry),
    )
    assert mismatch["promotion"]["state"] == "research_only"
    assert mismatch["promotion"]["reason"] == "reconciliation_error"


def _audit(strategy_id, actor="master", reason="reviewed evidence", signature="sig-324"):
    return {
        "schema": "strategy_promotion_audit_v1", "strategy_id": strategy_id,
        "actor": actor, "reason": reason, "signature": signature,
        "signed_at": datetime.now(timezone.utc).isoformat(),
    }


def test_every_manual_promotion_records_audit_and_rejects_incomplete_audit(
    tmp_path, verified_gate_factory
):
    registry = tmp_path / "strategy-registry.json"
    _register_legacy_gate(registry, verified_gate_factory)
    thresholds_path, _ = _thresholds(tmp_path)
    precommit = _precommit(thresholds_path)
    audit = _audit("strategy-a")
    sr.start_shadow("strategy-a", precommit=precommit, thresholds_path=str(thresholds_path),
                    promotion_audit=audit, registry_file=str(registry))
    shadow = _shadow(precommit, precommit["thresholds_sha256"])
    gate = _empirical_gate()
    sr.promote_strategy("strategy-a", "eligible_for_manual_pilot", empirical_gate=gate,
                        shadow_record=shadow, promotion_audit=audit,
                        registry_file=str(registry))
    approval = {"approved": True, "strategy_id": "strategy-a", "approver": "master",
                "approved_at": audit["signed_at"]}
    result = sr.promote_strategy("strategy-a", "manual_pilot", human_approval=approval,
                                 requested_weight=0.05, promotion_audit=audit,
                                 registry_file=str(registry))
    history = result["promotion"]["history"]
    assert [item["to"] for item in history] == ["shadow", "eligible_for_manual_pilot", "manual_pilot"]
    assert all(item["audit"]["signature"] == "sig-324" for item in history)
    with pytest.raises(ValueError, match="promotion_audit_invalid"):
        sr.promote_strategy("strategy-a", "live", empirical_gate=gate,
                            shadow_record=shadow,
                            promotion_audit={"actor": "master"}, registry_file=str(registry))


def test_research_gate_cli_requires_human_audit_and_rejects_missing_evidence(
    tmp_path, verified_gate_factory
):
    registry = tmp_path / "strategy-registry.json"
    _register_legacy_gate(registry, verified_gate_factory)
    thresholds_path, _ = _thresholds(tmp_path)
    precommit = _precommit(thresholds_path)
    precommit_path = tmp_path / "precommit.json"
    precommit_path.write_text(json.dumps(precommit), encoding="utf-8")
    args = ["start-shadow", "--strategy-id", "strategy-a", "--precommit", str(precommit_path),
            "--thresholds", str(thresholds_path), "--registry", str(registry),
            "--actor", "master", "--reason", "evidence reviewed", "--signature", "sig-324"]
    assert research_gate.main(args) == 0
    assert research_gate.main([
        "promote", "--strategy-id", "strategy-a", "--target", "eligible_for_manual_pilot",
        "--registry", str(registry), "--actor", "master", "--reason", "insufficient test",
        "--signature", "sig-324",
    ]) == 2


def _runner_args(registry, thresholds, *, precommit, empirical=None, shadow=None, dry_run=False,
                 paper_only=False, paper_weight=0.1):
    if isinstance(precommit, dict):
        precommit_path = registry.parent / "precommit.json"
        precommit_path.write_text(json.dumps(precommit), encoding="utf-8")
    else:
        precommit_path = precommit
    return type("Args", (), {
        "registry": str(registry),
        "strategy_id": None,
        "precommit": str(precommit_path),
        "thresholds": str(thresholds),
        "empirical_gate": str(empirical) if empirical else None,
        "shadow_record": str(shadow) if shadow else None,
        "dry_run": dry_run,
        "paper_only": paper_only,
        "paper_weight": paper_weight,
    })()


def test_automatic_runner_uses_start_shadow_gate_not_live_admission_bit(tmp_path):
    registry = tmp_path / "registry.json"
    thresholds_path, _ = _thresholds(tmp_path)
    precommit = _precommit(thresholds_path)
    # This is deliberately not live-admitted. start_shadow only gates on the
    # validated OOS precommit, so the runner must not silently skip it.
    registry.write_text(json.dumps({"strategy-a": {
        "strategy_id": "strategy-a", "allowed_in_live_agent": False,
        "evidence_verified": False, "promotion": {"state": "research_only"},
    }}), encoding="utf-8")

    result = promotion_runner.run(_runner_args(
        registry, thresholds_path, precommit=precommit,
    ))
    assert result["results"][0]["outcome"] == "promoted"
    assert sr.promotion_state("strategy-a", str(registry))["state"] == "shadow"


def test_automatic_runner_is_one_step_idempotent_and_never_manual_or_live(
    tmp_path, verified_gate_factory
):
    registry = tmp_path / "registry.json"
    _register_legacy_gate(registry, verified_gate_factory)
    thresholds_path, _ = _thresholds(tmp_path)
    precommit = _precommit(thresholds_path)
    first = promotion_runner.run(_runner_args(
        registry, thresholds_path, precommit=precommit,
    ))
    assert first["results"][0]["outcome"] == "promoted"
    # With no settlement artifacts the next run records an auditable skip and
    # does not jump over the eligible gate.
    second = promotion_runner.run(_runner_args(
        registry, thresholds_path, precommit=precommit,
    ))
    assert second["results"][0]["outcome"] == "skipped"
    assert sr.promotion_state("strategy-a", str(registry))["state"] == "shadow"
    history = sr.get("strategy-a", str(registry))["promotion"]["history"]
    assert history[-1]["outcome"] == "skipped"
    assert all(item.get("to") not in {"manual_pilot", "live"} for item in history)


def test_automatic_runner_dry_run_is_json_and_does_not_write_state(tmp_path):
    registry = tmp_path / "registry.json"
    thresholds_path, _ = _thresholds(tmp_path)
    precommit = _precommit(thresholds_path)
    registry.write_text(json.dumps({"strategy-a": {
        "strategy_id": "strategy-a", "promotion": {"state": "research_only"},
    }}), encoding="utf-8")
    before = registry.read_text(encoding="utf-8")
    result = promotion_runner.run(_runner_args(
        registry, thresholds_path, precommit=precommit, dry_run=True,
    ))
    assert result["schema"] == "strategy_promotion_run_v1"
    assert result["results"][0]["outcome"] == "would_promote"
    assert registry.read_text(encoding="utf-8") == before


def test_paper_runner_can_auto_promote_eligible_to_manual_pilot_without_real_runtime(
    tmp_path, verified_gate_factory
):
    registry = tmp_path / "registry.json"
    _register_legacy_gate(registry, verified_gate_factory)
    thresholds_path, _ = _thresholds(tmp_path)
    precommit = _precommit(thresholds_path)
    promotion_runner.run(_runner_args(registry, thresholds_path, precommit=precommit))
    shadow = _shadow(precommit, precommit["thresholds_sha256"])
    gate = _empirical_gate()
    empirical_path = tmp_path / "empirical.json"
    shadow_path = tmp_path / "shadow.json"
    empirical_path.write_text(json.dumps(gate), encoding="utf-8")
    shadow_path.write_text(json.dumps(shadow), encoding="utf-8")
    promotion_runner.run(_runner_args(
        registry, thresholds_path, precommit=precommit, empirical=empirical_path,
        shadow=shadow_path,
    ))
    result = promotion_runner.run(_runner_args(
        registry, thresholds_path, precommit=precommit, empirical=empirical_path,
        shadow=shadow_path, paper_only=True,
    ))
    assert result["mode"] == "paper_only"
    assert result["results"][0]["target"] == "manual_pilot"
    record = sr.live_record("strategy-a", str(registry))
    assert record["paper_runtime_allowed"] is True
    assert record["runtime_allowed"] is False
    assert sr.paper_live_weight("strategy-a", str(registry)) == pytest.approx(0.1)
    assert sr.is_allowed_in_live("strategy-a", str(registry)) is False
    assert sr.live_weight("strategy-a", str(registry)) == 0.0
