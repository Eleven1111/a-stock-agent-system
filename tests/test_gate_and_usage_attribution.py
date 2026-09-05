"""Gate accounting, usage attribution, and an eval that admits it did not run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import evaluate_openclaw_host as host_eval
from skills.common.gate_inventory import (
    KIND_EXPLANATORY,
    KIND_HARD,
    KIND_UNPROVEN,
    GateInventoryError,
    account_for_gate,
    build_inventory,
    evaluate_gate_contribution,
)
from skills.common.usage_attribution import (
    UNKNOWN,
    attribute_runs,
    deduplicate_usage,
    deterministic_module_costs,
    module_effectiveness,
    summarise_cost,
)

ROOT = Path(__file__).resolve().parents[1]


def _counts(**overrides):
    counts = {
        "candidates_before_gate": 100,
        "blocked_by_rule": 30,
        "blocked_by_missing_data": 10,
        "blocked_by_late_arrival": 5,
        "rejected_at_execution": 5,
        "terminal_outcomes": 45,
        "unresolved": 5,
    }
    counts.update(overrides)
    return counts


def _deltas(**overrides):
    deltas = {
        "net_return": 0.012, "max_drawdown": -0.03,
        "turnover": 1.4, "capital_exposure": 0.62,
    }
    deltas.update(overrides)
    return deltas


def test_the_inventory_separates_hard_constraints_from_unproven_filters():
    inventory = build_inventory([
        {"gate_id": "t_plus_one", "kind": KIND_HARD, "module": "execution_constraints"},
        {"gate_id": "chanlun_filter", "kind": KIND_UNPROVEN, "module": "paper_trading"},
        {"gate_id": "mfi_threshold", "kind": KIND_UNPROVEN, "module": "scoring"},
        {"gate_id": "evidence_note", "kind": KIND_EXPLANATORY, "affects_ranking": False},
    ])

    assert inventory["counts_by_kind"] == {
        KIND_HARD: 1, KIND_UNPROVEN: 2, KIND_EXPLANATORY: 1,
    }
    assert inventory["ablatable_gates"] == ["chanlun_filter", "mfi_threshold"]
    assert inventory["misfiled_explanatory_gates"] == []
    # The first deliverable is a list. Nothing in production moved.
    assert inventory["production_rules_changed"] is False
    hard = next(row for row in inventory["gates"] if row["gate_id"] == "t_plus_one")
    assert hard["kept_in_every_arm"] is True
    assert hard["ablatable"] is False


def test_an_explanatory_gate_that_moves_the_ranking_is_flagged_as_misfiled():
    inventory = build_inventory([
        {"gate_id": "commentary", "kind": KIND_EXPLANATORY, "affects_ranking": True},
    ])
    assert inventory["misfiled_explanatory_gates"] == ["commentary"]


def test_an_unknown_gate_kind_is_refused():
    with pytest.raises(GateInventoryError, match="unknown_gate_kind"):
        build_inventory([{"gate_id": "x", "kind": "probably_fine"}])


def test_every_candidate_that_entered_a_gate_has_to_come_out_somewhere():
    balanced = account_for_gate("chanlun_filter", _counts())
    assert balanced["denominator_balanced"] is True
    assert balanced["unaccounted"] == 0

    leaking = account_for_gate("chanlun_filter", _counts(terminal_outcomes=20))
    assert leaking["denominator_balanced"] is False
    assert leaking["unaccounted"] == 25


def test_incomplete_gate_counts_are_refused_rather_than_defaulted_to_zero():
    incomplete = _counts()
    del incomplete["blocked_by_late_arrival"]
    del incomplete["unresolved"]
    with pytest.raises(GateInventoryError, match="gate_counts_incomplete"):
        account_for_gate("chanlun_filter", incomplete)


def test_a_gate_contribution_refuses_to_conclude_on_an_unbalanced_ledger():
    accounting = account_for_gate("chanlun_filter", _counts(terminal_outcomes=20))
    result = evaluate_gate_contribution(
        "chanlun_filter", accounting, _deltas(), cohort_id="c1"
    )

    assert result["status"] == "not_evaluated"
    assert result["reason"] == "denominator_unbalanced"


def test_a_gate_contribution_refuses_to_conclude_on_partial_deltas():
    accounting = account_for_gate("chanlun_filter", _counts())
    partial = _deltas()
    del partial["turnover"]
    result = evaluate_gate_contribution(
        "chanlun_filter", accounting, partial, cohort_id="c1"
    )

    assert result["status"] == "not_evaluated"
    assert result["missing_deltas"] == ["turnover"]


def test_a_complete_gate_contribution_stays_research_only():
    accounting = account_for_gate("chanlun_filter", _counts())
    result = evaluate_gate_contribution(
        "chanlun_filter", accounting, _deltas(rejected_but_rose=7), cohort_id="c1"
    )

    assert result["status"] == "evaluated"
    assert result["deltas"]["net_return"] == pytest.approx(0.012)
    assert result["rejected_but_rose"] == 7
    # Rising after rejection is not automatically a miss.
    assert result["miss_requires"] == [
        "was_fillable", "capital_available", "holding_period_matched",
    ]
    assert result["production_rule_changed"] is False


def test_a_parent_and_its_subtasks_are_not_both_counted():
    records = [
        {"run_id": "parent", "input_tokens": 100},
        {"run_id": "child-a", "parent_run_id": "parent", "input_tokens": 40},
        {"run_id": "child-b", "parent_run_id": "parent", "input_tokens": 60},
        {"run_id": "standalone", "input_tokens": 10},
    ]
    folded = deduplicate_usage(records)

    assert folded["folded_into_parent"] == ["child-a", "child-b"]
    assert [row["run_id"] for row in folded["kept"]] == ["parent", "standalone"]


def test_usage_binds_to_business_runs_and_reports_its_coverage():
    attribution = attribute_runs(
        [
            {"run_id": "r1", "correlation_id": "c1", "input_tokens": 100,
             "output_tokens": 20, "cache_tokens": 5, "billed_amount": 0.02},
            {"run_id": "r2", "correlation_id": "c-missing", "input_tokens": 50},
        ],
        [{"correlation_id": "c1", "task_id": "t1", "role": "fundamental", "job_id": "j1"}],
    )

    assert [row["task_id"] for row in attribution["bound"]] == ["t1"]
    assert attribution["unbound_run_ids"] == ["r2"]
    assert attribution["attribution_coverage"] == pytest.approx(0.5)


def test_an_absent_price_stays_unknown_and_never_becomes_zero():
    attribution = attribute_runs(
        [{"run_id": "r1", "correlation_id": "c1", "input_tokens": 100}],
        [{"correlation_id": "c1", "task_id": "t1"}],
    )
    summary = summarise_cost(attribution, adopted_results=3)

    assert summary["billed_total"] == UNKNOWN
    assert summary["cost_basis"] == UNKNOWN
    assert summary["cost_per_adopted_result"] == UNKNOWN
    assert summary["unpriced_runs"] == 1
    assert summary["non_model_cost"] == UNKNOWN


def test_cost_per_adopted_result_is_undefined_when_nothing_was_adopted():
    attribution = attribute_runs(
        [{"run_id": "r1", "correlation_id": "c1", "billed_amount": 0.5}],
        [{"correlation_id": "c1", "task_id": "t1"}],
    )
    assert summarise_cost(attribution, adopted_results=0)["cost_per_adopted_result"] == (
        "undefined"
    )
    assert summarise_cost(attribution, adopted_results=2)["cost_per_adopted_result"] == (
        pytest.approx(0.25)
    )


def test_module_rates_stay_unknown_on_a_zero_base():
    zero_base = module_effectiveness("sector-crowding-daily", {
        "planned_occurrences": 0, "completed_valid": 0, "on_time_valid": 0,
        "consumed": 0, "adopted": 0,
    })
    assert zero_base["completed_valid_rate"] == UNKNOWN
    assert zero_base["adoption_rate"] == UNKNOWN

    real = module_effectiveness(
        "sector-crowding-daily",
        {"planned_occurrences": 20, "completed_valid": 18, "on_time_valid": 15,
         "consumed": 9, "adopted": 2, "consumed_display": 9, "consumed_experiment": 1},
        latencies={"scheduled_to_start_p50": 3.0},
    )
    assert real["completed_valid_rate"] == pytest.approx(0.9)
    assert real["on_time_valid_rate"] == pytest.approx(0.75)
    assert real["consumption_by_kind"]["display"] == 9
    assert real["consumption_by_kind"]["incident_diagnosis"] is None
    assert real["latency_percentiles"] == {"scheduled_to_start_p50": 3.0}
    # Thirty quiet days is a review trigger, not an automatic shutdown.
    assert real["retirement_decision"] == "requires_dependency_closure_and_owner_review"


def test_a_deterministic_job_has_zero_model_tokens_but_unknown_other_costs():
    costs = deterministic_module_costs("sector-crowding-daily")

    assert costs["model_tokens"] == {"input_tokens": 0, "output_tokens": 0, "cache_tokens": 0}
    assert costs["model_token_basis"] == "no_model_call_in_this_job"
    assert costs["cpu_cost"] == UNKNOWN
    assert costs["data_fetch_cost"] == UNKNOWN


def test_the_host_eval_reports_not_run_instead_of_faking_a_model(monkeypatch):
    monkeypatch.setattr(host_eval.shutil, "which", lambda name: None)
    report = host_eval.evaluate()

    assert report["status"] == "not_run"
    assert report["reason"] == "openclaw_binary_not_found"
    assert report["metrics"] is None
    assert report["case_count"] == 20
    assert report["delivery_disabled"] is True
    assert report["runnable_entrypoint"].startswith("python scripts/evaluate_openclaw_host.py")


def test_an_available_host_without_observations_is_still_not_run(monkeypatch):
    monkeypatch.setattr(host_eval, "host_availability", lambda *a, **k: {
        "available": True, "binary": "/usr/local/bin/openclaw", "version": "9.9.9",
    })
    report = host_eval.evaluate()

    assert report["status"] == "not_run"
    assert report["reason"] == "no_observations_supplied"


def test_the_host_eval_keeps_citation_resolution_and_support_apart(monkeypatch):
    monkeypatch.setattr(host_eval, "host_availability", lambda *a, **k: {"available": True})
    observations = [
        {"id": "normal-01", "citations_resolvable": True, "citations_supporting": True,
         "judgements_offered": True, "independent_score_improved": True},
        {"id": "normal-02", "citations_resolvable": True, "citations_supporting": False,
         "judgements_offered": True},
        {"id": "insufficient-01", "grounded_abstentions": True},
        {"id": "toolfail-01", "technical_failures": True},
    ]
    report = host_eval.evaluate(observations=observations)
    metrics = report["metrics"]

    assert report["status"] == "ok"
    # A citation that resolves is not a citation that supports the claim.
    assert metrics["counts"]["citations_resolvable"] == 2
    assert metrics["counts"]["citations_supporting"] == 1
    assert metrics["citation_support_gap"] == 1
    assert metrics["abstention_split"] == {
        "grounded": 1, "unfounded": 0, "technical_failure": 1,
    }
    assert metrics["judgement_without_score_improvement"] == 1
    assert metrics["scope"] == "engineering_integration_only"
    assert "strategy_validity" in metrics["not_a_claim_about"]


def test_the_case_set_is_twenty_stratified_tasks():
    document = host_eval.load_cases()

    assert len(document["cases"]) == 20
    assert sum(document["strata"].values()) == 20
    assert document["scope"] == "engineering_integration_only"
    strata_seen = {case["stratum"] for case in document["cases"]}
    assert strata_seen == set(document["strata"])


def test_the_frozen_harness_reports_real_fact_plane_attempts():
    from scripts import evaluate_agent_harness

    metrics = evaluate_agent_harness.evaluate()["metrics"]["fact_plane_writes"]

    # The old hardcoded zero hid eight declared attempts.
    assert metrics["attempts_declared"] == 8
    assert metrics["blocked_attempts"] == 8
    assert metrics["completed_writes"] == 0
    assert metrics["guarantee_scope"] == "static_protocol_only"
    assert metrics["not_evidence_of"] == "operating_system_level_write_isolation"


def test_the_case_file_on_disk_stays_valid_json():
    document = json.loads(
        (ROOT / "evals" / "openclaw_host" / "cases.json").read_text(encoding="utf-8")
    )
    assert document["schema"] == "openclaw_host_eval_cases_v1"
