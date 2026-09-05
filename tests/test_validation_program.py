import hashlib
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from validation_program import (
    DailyEvidenceRegistry,
    OOSRegistry,
    ShadowWindowRegistry,
    ValidationError,
    block_bootstrap_mean,
    build_shadow_run_artifact,
    build_validation_report,
    build_walk_forward_folds,
    compute_effective_samples,
    compute_statistical_validation,
    deflated_sharpe,
    evaluate_empirical_gate,
    fdr_benjamini_hochberg,
    hac_mean_uncertainty,
    load_validation_thresholds,
    probability_of_backtest_overfitting,
    reconcile_broker_statement,
    verify_validation_artifact,
)
from a_share_rules import is_trading_day


def _write_pit_snapshot(path, day, payload=None):
    payload = payload or {"day": day, "quotes": {"600001": {"price": 10.0}}}
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    payload_hash = hashlib.sha256(canonical).hexdigest()
    record = {
        "schema": "market_snapshot_v1",
        "snapshot_id": f"snap-{payload_hash[:24]}",
        "snapshot_path": str(path),
        "payload_hash": payload_hash,
        "payload": payload,
        "point_in_time": {
            "schema": "pit_stage_contract_v1",
            "decision_mode": "live",
            "event_asof": day,
            "evidence_time": f"{day}T15:00:00+08:00",
            "captured_at": f"{day}T15:00:00+08:00",
            "stage_policy": {
                "schema": "pit_stage_contract_v1",
                "stage": "daily_validation",
                "cutoff_time": "15:30:00",
                "timezone": "Asia/Shanghai",
                "publication_delay_seconds": 0,
            },
        },
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()


def _clean_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    rules = repo / "rules.json"
    dataset = repo / "dataset.json"
    thresholds = repo / "thresholds.json"
    rules.write_text('{"rule":"v1"}', encoding="utf-8")
    dataset.write_text('{"rows":[1,2,3]}', encoding="utf-8")
    thresholds.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "shadow": {
                    "minimum_trading_days": 2,
                    "maximum_simulation_error": 0.02,
                    "auto_demotion_error": 0.05,
                    "maximum_manual_pilot_weight": 0.1,
                },
            }
        ),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    return repo, rules, dataset, thresholds


def _persist_precommit_independent(
    registry_path, repo, invocation_id, rules, dataset, split, variants, folds,
    thresholds,
):
    common_path = Path(__file__).resolve().parents[1] / "skills" / "common"
    script = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from validation_program import OOSRegistry
OOSRegistry(sys.argv[2], sys.argv[3], invocation_id=sys.argv[4]).create_precommit(
    sys.argv[5], sys.argv[6], split=json.loads(sys.argv[7]),
    variants=json.loads(sys.argv[8]), fold_ids=json.loads(sys.argv[9]),
    thresholds_path=sys.argv[10],
)
"""
    subprocess.run(
        [
            sys.executable, "-c", script, str(common_path), str(registry_path),
            str(repo), invocation_id, str(rules), str(dataset), json.dumps(split),
            json.dumps(variants), json.dumps(folds), str(thresholds),
        ],
        check=True,
    )
    return next(
        json.loads(line)
        for line in registry_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("record_type") == "precommit"
        and json.loads(line).get("invocation_id") == invocation_id
    )


def _shadow_precommit(tmp_path, *, invocation_id="precommit-run", variants=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo, rules, dataset, thresholds = _clean_repo(tmp_path)
    registry_path = tmp_path / "precommit-registry.jsonl"
    split = {"method": "walk_forward", "purge": 1, "embargo": 1}
    variant_set = variants or ["base", "ablation"]
    folds = ["fold-0", "fold-1"]
    precommit = _persist_precommit_independent(
        registry_path, repo, invocation_id, rules, dataset, split, variant_set,
        folds, thresholds,
    )
    start_args = {
        "precommit_registry_path": registry_path,
        "precommit_id": precommit["precommit_id"],
        "invocation_id": "shadow-run",
        "repo_root": repo,
        "rules_path": rules,
        "dataset_path": dataset,
        "expected_split": split,
        "expected_variants": variant_set,
        "expected_fold_ids": folds,
    }
    return repo, rules, dataset, thresholds, registry_path, precommit, start_args


def test_oos_registry_requires_prior_durable_invocation_and_exact_variants(tmp_path):
    repo, rules, dataset, thresholds = _clean_repo(tmp_path)
    registry_path = tmp_path / "registry.jsonl"
    split = {"method": "walk_forward", "purge": 1, "embargo": 1}
    precommit = _persist_precommit_independent(
        registry_path,
        repo,
        "precommit-run",
        rules,
        dataset,
        split,
        ["base", "ablation"],
        ["fold-0", "fold-1"],
        thresholds,
    )
    first = OOSRegistry(registry_path, repo, invocation_id="precommit-run")

    with pytest.raises(ValidationError, match="same_run_reveal"):
        first.register_result(
            precommit["precommit_id"], rules, dataset,
            variant_results={"base": {"status": "passed"}, "ablation": {"status": "failed"}},
            fold_results={"fold-0": {"status": "passed"}, "fold-1": {"status": "failed"}},
            thresholds_path=thresholds,
        )

    reveal = OOSRegistry(registry_path, repo, invocation_id="reveal-run")
    with pytest.raises(ValidationError, match="variant_missing"):
        reveal.register_result(
            precommit["precommit_id"], rules, dataset,
            variant_results={"base": {"status": "passed"}},
            fold_results={"fold-0": {"status": "passed"}, "fold-1": {"status": "failed"}},
            thresholds_path=thresholds,
        )

    result = reveal.register_result(
        precommit["precommit_id"], rules, dataset,
        variant_results={"base": {"status": "passed"}, "ablation": {"status": "failed"}},
        fold_results={"fold-0": {"status": "passed"}, "fold-1": {"status": "failed"}},
        thresholds_path=thresholds,
    )
    assert result["status"] == "registered"
    assert result["artifact_sha256"]
    assert [item["variant_id"] for item in result["variants"]] == ["ablation", "base"]
    assert [item["fold_id"] for item in result["folds"]] == ["fold-0", "fold-1"]

    third = OOSRegistry(registry_path, repo, invocation_id="second-reveal")
    with pytest.raises(ValidationError, match="duplicate_reveal"):
        third.register_result(
            precommit["precommit_id"], rules, dataset,
            variant_results={"base": {"status": "passed"}, "ablation": {"status": "failed"}},
            fold_results={"fold-0": {"status": "passed"}, "fold-1": {"status": "failed"}},
            thresholds_path=thresholds,
        )


def test_oos_registry_rejects_same_process_with_different_invocation_label(tmp_path):
    repo, rules, dataset, thresholds = _clean_repo(tmp_path)
    registry_path = tmp_path / "same-process.jsonl"
    precommit = OOSRegistry(
        registry_path, repo, invocation_id="precommit-label"
    ).create_precommit(
        rules,
        dataset,
        split={"method": "fixed"},
        variants=["base"],
        fold_ids=["fold-0"],
        thresholds_path=thresholds,
    )
    with pytest.raises(ValidationError, match="same_run_reveal"):
        OOSRegistry(
            registry_path, repo, invocation_id="different-label"
        ).register_result(
            precommit["precommit_id"],
            rules,
            dataset,
            variant_results={"base": {"status": "passed"}},
            fold_results={"fold-0": {"status": "passed"}},
            thresholds_path=thresholds,
        )


def test_oos_registry_detects_dirty_precommit_and_tampering(tmp_path):
    repo, rules, dataset, thresholds = _clean_repo(tmp_path)
    registry_path = tmp_path / "registry.jsonl"
    rules.write_text('{"rule":"dirty"}', encoding="utf-8")
    with pytest.raises(ValidationError, match="dirty_tree"):
        OOSRegistry(registry_path, repo, invocation_id="dirty").create_precommit(
            rules, dataset, split={"method": "fixed"}, variants=["base"],
            fold_ids=["fold-0"], thresholds_path=thresholds,
        )
    _git(repo, "checkout", "--", "rules.json")
    precommit = _persist_precommit_independent(
        registry_path, repo, "pre", rules, dataset, {"method": "fixed"},
        ["base"], ["fold-0"], thresholds,
    )
    rules.write_text('{"rule":"tampered"}', encoding="utf-8")
    with pytest.raises(ValidationError, match="artifact_tampered"):
        OOSRegistry(registry_path, repo, invocation_id="post").register_result(
            precommit["precommit_id"], rules, dataset,
            variant_results={"base": {"status": "passed"}},
            fold_results={"fold-0": {"status": "passed"}},
            thresholds_path=thresholds,
        )


def test_oos_registry_rejects_threshold_tamper_and_non_descendant(tmp_path):
    repo, rules, dataset, thresholds = _clean_repo(tmp_path)
    registry_path = tmp_path / "registry.jsonl"
    precommit = _persist_precommit_independent(
        registry_path, repo, "pre", rules, dataset, {"method": "fixed"},
        ["base"], ["fold-0"], thresholds,
    )
    thresholds.write_text('{"schema_version":"tampered"}', encoding="utf-8")
    with pytest.raises(ValidationError, match="artifact_tampered"):
        OOSRegistry(registry_path, repo, invocation_id="reveal").register_result(
            precommit["precommit_id"], rules, dataset,
            variant_results={"base": {"status": "passed"}},
            fold_results={"fold-0": {"status": "passed"}}, thresholds_path=thresholds,
        )


def test_walk_forward_purge_embargo_and_failed_fold_reporting():
    folds = build_walk_forward_folds(
        30, train_size=10, calibration_size=2, test_size=4, step=4, purge=2, embargo=1, mode="expanding"
    )
    assert folds[0] == {
        "fold_id": "fold-0", "train_start": 0, "train_end": 10,
        "calibration_start": 12, "calibration_end": 14,
        "test_start": 16, "test_end": 20, "purge": 2, "embargo": 1,
        "roles": ["train", "calibration", "test"],
    }
    assert all(fold["train_end"] + 2 <= fold["test_start"] for fold in folds)
    assert all(
        folds[index + 1]["test_start"] >= folds[index]["test_end"] + 1
        for index in range(len(folds) - 1)
    )
    rolling = build_walk_forward_folds(
        40, train_size=10, calibration_size=2, test_size=4, step=4, purge=1, embargo=1, mode="rolling"
    )
    assert rolling[1]["train_start"] > 0
    with pytest.raises(ValidationError, match="walk_forward_invalid"):
        build_walk_forward_folds(10, train_size=8, calibration_size=1, test_size=4, step=4, purge=0, embargo=0)


def test_repository_computes_cluster_effective_samples():
    trades = [
        {
            "trade_id": f"t{i}",
            "stock": stock,
            "session": f"2026-07-{i + 1:02d}",
            "regime": regime,
        }
        for i, (stock, regime) in enumerate(
            [("A", "up"), ("A", "up"), ("A", "up"), ("A", "down"), ("B", "down"), ("B", "down")]
        )
    ]
    samples = compute_effective_samples(trades)
    assert samples["trade"] == 6.0
    assert samples["stock"] == pytest.approx(1.8)
    assert samples["regime"] == pytest.approx(2.0)
    assert samples["session"] == pytest.approx(6.0)
    assert samples["basis"] == "kish_breadth"
    assert samples["status"] == "evaluated"
    assert samples["input_sha256"]
    assert compute_effective_samples([])["status"] == "not_evaluated"


def test_time_series_statistics_are_computed_and_bound():
    returns = [0.01, -0.005, 0.02, 0.0, -0.01, 0.015, 0.005, -0.002] * 4
    bootstrap = block_bootstrap_mean(returns, block_length=4, resamples=200, seed=7)
    hac = hac_mean_uncertainty(returns, lags=3)
    fdr = fdr_benjamini_hochberg({"a": 0.01, "b": 0.04, "c": 0.2}, alpha=0.05)
    pbo = probability_of_backtest_overfitting(
        {
            "a": [0.03, 0.02, -0.01, 0.01, 0.02, -0.02, 0.01, 0.0],
            "b": [0.01, 0.0, 0.02, -0.01, 0.0, 0.01, -0.02, 0.02],
            "c": [-0.01, 0.01, 0.0, 0.02, -0.02, 0.0, 0.03, -0.01],
        },
        partitions=4,
    )
    dsr = deflated_sharpe(returns, trials=5, trial_sharpes=[0.05, 0.11, 0.18, 0.26, 0.40])
    assert bootstrap["status"] == hac["status"] == pbo["status"] == dsr["status"] == "evaluated"
    assert bootstrap["method"] == "moving_block_bootstrap"
    assert hac["method"] == "newey_west_hac"
    assert bootstrap["input_sha256"] == hac["input_sha256"] == dsr["input_sha256"]
    assert bootstrap["ci_low"] <= bootstrap["mean"] <= bootstrap["ci_high"]
    assert bootstrap["ci_low"] == pytest.approx(0.0028078125)
    assert bootstrap["ci_high"] == pytest.approx(0.00547265625)
    assert hac["standard_error"] == pytest.approx(0.0007977075154926381)
    assert fdr["discoveries"] == ["a"]
    assert fdr["adjusted_p_values"] == pytest.approx({"a": 0.03, "b": 0.06, "c": 0.2})
    # 0.75, not the pre-2026-09-05 1.0: one fold ties in-sample and now shares
    # the outcome across the tied set instead of picking the alphabetical winner.
    assert pbo["pbo"] == pytest.approx(0.75)
    assert pbo["method_version"] == "cscv-v2"
    assert dsr["method_version"] == "deflated_sharpe-v2"
    assert deflated_sharpe(returns, trials=5)["reason"] == "trial_dispersion_unavailable"
    assert block_bootstrap_mean([0.1], block_length=2)["status"] == "not_evaluated"
    assert hac_mean_uncertainty([1.0] * 20, lags=2)["status"] == "not_evaluated"

    suite = compute_statistical_validation(
        primary_variant="a",
        variant_returns={
            "a": returns,
            "b": [-value for value in returns],
        },
        p_values={"a": 0.01, "b": 0.2},
        config={
            "minimum_observations": 20, "block_length": 4, "bootstrap_resamples": 200,
            "fdr_alpha": 0.05, "hac_lags": 3, "pbo_partitions": 4,
            "maximum_pbo": 0.5, "minimum_deflated_sharpe_probability": 0.95,
        },
        seed=7,
    )
    assert suite["status"] in {"passed", "failed"}
    assert suite["artifact_sha256"]


def test_daily_evidence_is_immutable_and_empirical_gate_fails_closed(tmp_path):
    artifact = tmp_path / "pit.json"
    _write_pit_snapshot(artifact, "2026-07-01")
    registry = DailyEvidenceRegistry(tmp_path / "daily.jsonl")
    first = registry.append("2026-07-01", artifact, event_asof="2026-07-01T15:00:00+08:00")
    assert registry.append("2026-07-01", artifact, event_asof="2026-07-01T15:00:00+08:00") == first
    _write_pit_snapshot(artifact, "2026-07-01", payload={"changed": True})
    with pytest.raises(ValidationError, match="daily_evidence_conflict"):
        registry.append("2026-07-01", artifact, event_asof="2026-07-01T15:00:00+08:00")
    report = registry.coverage_report()
    assert report["real_trading_days"] == 1
    assert Path(first["content_addressed_path"]).is_file()
    assert report["invalid_or_missing_artifacts"] == 0

    gate = evaluate_empirical_gate(
        registry.records(),
        trades=[{
            "trade_id": "t1", "stock": "A", "session": "2026-07-01", "regime": "up"
        }],
        statistics={"block_bootstrap": {"status": "not_evaluated"}},
        shadow={"status": "not_evaluated"},
        broker={"status": "not_evaluated"},
        thresholds={
            "minimum_real_trading_days": 60,
            "minimum_trade_effective_samples": 30,
            "minimum_stock_effective_samples": 8,
            "minimum_regime_effective_samples": 3,
        },
    )
    assert gate["status"] == "blocked"
    assert gate["production_release"] == "blocked"
    assert set(gate["reasons"]) >= {"<60_days", "independent_clusters_insufficient", "statistics_not_evaluated"}

    Path(first["content_addressed_path"]).unlink()
    missing_report = registry.coverage_report()
    assert missing_report["real_trading_days"] == 0
    assert missing_report["invalid_or_missing_artifacts"] == 1

    forged = evaluate_empirical_gate(
        [], trades=[],
        statistics={"status": "passed", "block_bootstrap": {"status": "evaluated"}},
        shadow={"status": "passed"}, broker={"status": "reconciled"},
        thresholds={
            "minimum_real_trading_days": 0, "minimum_trade_effective_samples": 0,
            "minimum_stock_effective_samples": 0, "minimum_regime_effective_samples": 0,
        },
    )
    assert forged["status"] == "blocked"
    assert set(forged["reasons"]) >= {
        "statistics_not_evaluated", "shadow_not_evaluated", "broker_reconciliation_missing"
    }


def test_daily_evidence_rejects_non_trading_day(tmp_path):
    artifact = tmp_path / "pit.json"
    artifact.write_text('{"point_in_time":true}', encoding="utf-8")
    registry = DailyEvidenceRegistry(tmp_path / "daily.jsonl")
    with pytest.raises(ValidationError, match="daily_evidence_not_trading_day"):
        registry.append(
            "2026-07-04", artifact, event_asof="2026-07-04T15:00:00+08:00"
        )


def test_complete_computed_empirical_evidence_can_pass(tmp_path):
    daily = DailyEvidenceRegistry(tmp_path / "daily.jsonl")
    sessions = []
    cursor = date(2026, 1, 5)
    while len(sessions) < 60:
        if is_trading_day(cursor):
            session = cursor.isoformat()
            sessions.append(session)
            artifact = tmp_path / f"pit-{session}.json"
            _write_pit_snapshot(artifact, session)
            daily.append(
                session,
                artifact,
                event_asof=f"{session}T15:00:00+08:00",
            )
        cursor += timedelta(days=1)

    returns = [0.01, 0.02, 0.015, 0.005, 0.012, 0.018, 0.008, 0.016] * 5
    statistics_artifact = compute_statistical_validation(
        primary_variant="a",
        variant_returns={"a": returns, "b": [-value for value in returns]},
        p_values={"a": 0.001, "b": 0.5},
        config={
            "minimum_observations": 20,
            "block_length": 4,
            "bootstrap_resamples": 200,
            "fdr_alpha": 0.05,
            "hac_lags": 3,
            "pbo_partitions": 4,
            "maximum_pbo": 0.5,
            "minimum_deflated_sharpe_probability": 0.95,
        },
        seed=7,
    )
    assert statistics_artifact["status"] == "passed"

    (
        _, _, _, thresholds_path, _, _, shadow_start_args
    ) = _shadow_precommit(tmp_path / "shadow-contract", variants=["base"])
    shadow_registry = ShadowWindowRegistry(tmp_path / "shadow.jsonl")
    shadow_registry.start(
        "strategy-a",
        thresholds_path,
        **shadow_start_args,
    )
    shadow_artifact = None
    for session in sessions[:2]:
        shadow_artifact = shadow_registry.observe(
            "strategy-a",
            thresholds_path,
            simulation_error=0.01,
            real_trading_day=True,
            trading_date=session,
        )
    assert shadow_artifact["status"] == "eligible_for_manual_pilot"
    assert verify_validation_artifact(shadow_artifact) is True

    statement = tmp_path / "broker.json"
    statement.write_text(
        json.dumps({
            "schema_version": "broker-statement-v1",
            "asof": f"{sessions[-1]}T15:00:00+08:00",
            "cash_balance": 1000.0,
            "positions": {"000001": 100},
        }),
        encoding="utf-8",
    )
    broker_artifact = reconcile_broker_statement(
        statement,
        ledger_summary={"cash_balance": 1000.0, "positions": {"000001": 100}},
        cash_tolerance=0.01,
    )
    trades = [
        {
            "trade_id": f"t{index}",
            "stock": f"{index % 10:06d}",
            "session": sessions[index],
            "regime": ("up", "flat", "down")[index % 3],
        }
        for index in range(30)
    ]
    gate = evaluate_empirical_gate(
        daily.records(),
        trades=trades,
        statistics=statistics_artifact,
        shadow=shadow_artifact,
        broker=broker_artifact,
        thresholds={
            "minimum_real_trading_days": 60,
            "minimum_trade_effective_samples": 30,
            "minimum_stock_effective_samples": 8,
            "minimum_regime_effective_samples": 3,
        },
    )
    assert gate["status"] == "passed"
    assert gate["production_release"] == "eligible_for_review"
    tampered = dict(statistics_artifact, status="passed", computed_by="caller")
    assert verify_validation_artifact(tampered) is False


def test_shadow_and_broker_artifacts_are_computed_not_attested(tmp_path):
    shadow = build_shadow_run_artifact(
        strategy_id="strategy-a",
        trading_date="2026-07-01",
        precommit_id="precommit-1",
        thresholds_sha256="a" * 64,
        simulated_orders=[{"stock": "000001", "side": "buy", "quantity": 100}],
        live_ranking_before=["000001", "000002"],
        live_ranking_after=["000001", "000002"],
    )
    assert shadow["live_effect"] == "none"
    assert shadow["artifact_sha256"]
    with pytest.raises(ValidationError, match="shadow_live_effect_detected"):
        build_shadow_run_artifact(
            strategy_id="strategy-a", trading_date="2026-07-01", precommit_id="precommit-1",
            thresholds_sha256="a" * 64, simulated_orders=[],
            live_ranking_before=["000001"], live_ranking_after=["000002"],
        )

    statement = tmp_path / "broker.json"
    statement.write_text(
        json.dumps(
            {
                "schema_version": "broker-statement-v1",
                "asof": "2026-07-01T15:00:00+08:00",
                "cash_balance": 1000.01,
                "positions": {"000001": 100, "000002": 0},
            }
        ),
        encoding="utf-8",
    )
    reconciled = reconcile_broker_statement(
        statement,
        ledger_summary={"cash_balance": 1000.0, "positions": {"000001": 100}},
        cash_tolerance=0.02,
    )
    assert reconciled["status"] == "reconciled"
    assert reconciled["statement_sha256"]
    mismatch = reconcile_broker_statement(
        statement,
        ledger_summary={"cash_balance": 900.0, "positions": {"000001": 99}},
        cash_tolerance=0.02,
    )
    assert mismatch["status"] == "mismatch"
    assert mismatch["position_mismatches"] == {"000001": {"broker": 100.0, "ledger": 99.0}}


def test_complete_report_includes_failures_risk_cost_and_capacity():
    report = build_validation_report(
        precommitted_variants=["base", "ablation"],
        precommitted_folds=["fold-0", "fold-1"],
        variant_results={"base": {"status": "passed"}, "ablation": {"status": "failed"}},
        fold_results={"fold-0": {"status": "passed"}, "fold-1": {"status": "failed"}},
        returns=[0.10, -0.05, -0.10, 0.04],
        weights=[{"A": 0.5}, {"A": 0.3, "B": 0.2}, {"B": 0.4}],
        cost_stress_bps=[0, 10, 50],
        capacity_inputs=[
            {"capital": 1_000_000, "required_notional": 100_000, "adv": 2_000_000},
            {"capital": 2_000_000, "required_notional": 200_000, "adv": 2_000_000},
        ],
        maximum_adv_participation=0.10,
    )
    assert report["status"] == "evaluated"
    assert report["variants"][1]["status"] == "passed"
    assert report["folds"][1]["status"] == "failed"
    assert report["turnover"] == pytest.approx(0.45)
    assert report["maximum_drawdown"] == pytest.approx(-0.145)
    assert report["tail_loss"] == pytest.approx(-0.10)
    assert report["cost_stress"]["50"] < report["cost_stress"]["0"]
    assert report["capacity_curve"][-1]["status"] == "at_limit"

    missing = build_validation_report(
        precommitted_variants=["base", "ablation"], precommitted_folds=["fold-0"],
        variant_results={"base": {"status": "passed"}}, fold_results={"fold-0": {"status": "passed"}},
        returns=[0.01, 0.02], weights=[], cost_stress_bps=[], capacity_inputs=None,
    )
    assert missing["status"] == "not_evaluated"
    assert set(missing["reasons"]) >= {"variant_missing", "cost_stress_missing", "capacity_unknown"}


def test_shadow_thresholds_are_precommitted_reset_and_auto_demote(tmp_path):
    (
        repo, rules, dataset, thresholds_path, registry_path, precommit, start_args
    ) = _shadow_precommit(tmp_path)
    state = ShadowWindowRegistry(tmp_path / "shadow.jsonl")
    started = state.start("strategy-a", thresholds_path, **start_args)
    assert started["status"] == "shadow"
    with pytest.raises(ValidationError, match="threshold_override"):
        state.observe(
            "strategy-a", thresholds_path, simulation_error=0.01,
            real_trading_day=True, trading_date="2026-07-01",
            threshold_overrides={"minimum_trading_days": 1},
        )
    day_one = state.observe(
        "strategy-a", thresholds_path, simulation_error=0.02,
        real_trading_day=True, trading_date="2026-07-01",
    )
    assert day_one["status"] == "shadow"
    duplicate = state.observe(
        "strategy-a", thresholds_path, simulation_error=0.02,
        real_trading_day=True, trading_date="2026-07-01",
    )
    assert duplicate["observed_trading_days"] == 1
    boundary = state.observe(
        "strategy-a", thresholds_path, simulation_error=0.02,
        real_trading_day=True, trading_date="2026-07-02",
    )
    assert boundary["status"] == "eligible_for_manual_pilot"
    demoted = state.observe(
        "strategy-a", thresholds_path, simulation_error=0.05,
        real_trading_day=True, trading_date="2026-07-03",
    )
    assert demoted["status"] == "research_only"
    assert demoted["reason"] == "auto_demoted"

    changed = json.loads(thresholds_path.read_text(encoding="utf-8"))
    changed["schema_version"] = "v2"
    changed["shadow"]["minimum_trading_days"] = 3
    thresholds_path.write_text(json.dumps(changed), encoding="utf-8")
    _git(repo, "add", "thresholds.json")
    _git(repo, "commit", "-qm", "change thresholds")
    second = _persist_precommit_independent(
        registry_path, repo, "precommit-v2", rules, dataset,
        start_args["expected_split"], start_args["expected_variants"],
        start_args["expected_fold_ids"], thresholds_path,
    )
    reset = state.start(
        "strategy-a", thresholds_path,
        **{
            **start_args,
            "precommit_id": second["precommit_id"],
            "invocation_id": "shadow-v2",
        },
    )
    assert reset["status"] == "shadow"
    assert reset["observed_trading_days"] == 0
    assert reset["reason"] == "shadow_window_reset"


def test_shadow_start_requires_prior_registry_and_exact_precommit_contract(tmp_path):
    (
        repo, rules, dataset, thresholds, registry_path, precommit, start_args
    ) = _shadow_precommit(tmp_path)
    state = ShadowWindowRegistry(tmp_path / "shadow.jsonl")

    local_root = tmp_path / "same-process"
    local_root.mkdir()
    local_repo, local_rules, local_dataset, local_thresholds = _clean_repo(local_root)
    local_registry = local_root / "precommit.jsonl"
    local_split = {"method": "fixed"}
    local_precommit = OOSRegistry(
        local_registry, local_repo, invocation_id="local-create"
    ).create_precommit(
        local_rules, local_dataset, split=local_split, variants=["base"],
        fold_ids=["fold-0"], thresholds_path=local_thresholds,
    )
    with pytest.raises(ValidationError, match="same_run_reveal"):
        state.start(
            "strategy-local", local_thresholds,
            precommit_registry_path=local_registry,
            precommit_id=local_precommit["precommit_id"],
            invocation_id="different-caller-label",
            repo_root=local_repo,
            rules_path=local_rules,
            dataset_path=local_dataset,
            expected_split=local_split,
            expected_variants=["base"],
            expected_fold_ids=["fold-0"],
        )

    with pytest.raises(ValidationError, match="same_run_reveal"):
        state.start(
            "strategy-a", thresholds,
            **{**start_args, "invocation_id": "precommit-run"},
        )
    with pytest.raises(ValidationError, match="precommit_missing"):
        state.start(
            "strategy-a", thresholds,
            **{**start_args, "precommit_id": "caller-invented"},
        )
    with pytest.raises(ValidationError, match="split_mismatch"):
        state.start(
            "strategy-a", thresholds,
            **{**start_args, "expected_split": {"method": "fixed"}},
        )
    with pytest.raises(ValidationError, match="variant_missing"):
        state.start(
            "strategy-a", thresholds,
            **{**start_args, "expected_variants": ["base"]},
        )

    original_branch = _git(repo, "branch", "--show-current")
    _git(repo, "checkout", "-q", "--orphan", "unrelated")
    _git(repo, "commit", "-qm", "unrelated history")
    with pytest.raises(ValidationError, match="ancestry_invalid"):
        state.start("strategy-a", thresholds, **start_args)
    _git(repo, "checkout", "-q", original_branch)

    started = state.start("strategy-a", thresholds, **start_args)
    assert started["precommit_id"] == precommit["precommit_id"]
    rules.write_text('{"rule":"v2"}', encoding="utf-8")
    _git(repo, "add", "rules.json")
    _git(repo, "commit", "-qm", "change rules")
    next_precommit = _persist_precommit_independent(
        registry_path, repo, "precommit-next", rules, dataset,
        start_args["expected_split"], start_args["expected_variants"],
        start_args["expected_fold_ids"], thresholds,
    )
    next_args = {
        **start_args,
        "precommit_id": next_precommit["precommit_id"],
        "invocation_id": "shadow-next",
    }
    reset = state.start("strategy-a", thresholds, **next_args)
    assert reset["reason"] == "shadow_window_reset"
    assert reset["precommit_id"] == next_precommit["precommit_id"]
    dataset.write_text('{"rows":[999]}', encoding="utf-8")
    with pytest.raises(ValidationError, match="artifact_tampered"):
        state.start("strategy-b", thresholds, **next_args)


def test_shadow_observation_requires_unique_real_trading_session(tmp_path):
    *_, thresholds, _, _, start_args = _shadow_precommit(tmp_path)
    state = ShadowWindowRegistry(tmp_path / "shadow.jsonl")
    state.start("strategy-a", thresholds, **start_args)
    with pytest.raises(ValidationError, match="shadow_trading_date_required"):
        state.observe(
            "strategy-a", thresholds, simulation_error=0.01, real_trading_day=True
        )
    with pytest.raises(ValidationError, match="shadow_not_trading_day"):
        state.observe(
            "strategy-a", thresholds, simulation_error=0.01,
            real_trading_day=True, trading_date="2026-07-04",
        )


def test_repository_validation_thresholds_are_versioned_and_valid():
    config = load_validation_thresholds("config/validation_thresholds.json")
    assert config["schema_version"] == "validation-thresholds-v1"
    assert config["config_sha256"]
