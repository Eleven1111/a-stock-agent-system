"""One named exploratory experiment, end to end, without weakening real promotion.

The gold line under test: frozen experiment -> admission -> ranking -> budget ->
executable simulation -> run record with fills, rejections and unresolved cases
all still in the denominator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skills.common import exploratory_paper_experiment as experiment_module
from skills.common.exploratory_paper_experiment import (
    ENTRY_POINT_EXPLORATORY,
    ENTRY_POINT_PILOT,
    WEIGHT_SEMANTICS,
    ExperimentError,
    admit,
    freeze_experiment,
    rank_candidates,
    scope_idempotency_key,
    select_within_budget,
    simulate_admitted,
    summarise_run,
)
from skills.common.research_artifact import json_sha256

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config" / "exploratory_paper_experiments.json"


def _spec(**overrides):
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    spec = dict(catalog["experiments"][0])
    spec.update(overrides)
    return spec


def _scopes():
    return json.loads(CATALOG.read_text(encoding="utf-8"))["registered_exploratory_scopes"]


def _bars(code_base: float):
    return [
        {"trading_date": "2026-09-08", "open": code_base, "high": code_base + 0.4,
         "low": code_base - 0.2, "close": code_base + 0.3, "amount": 5e8},
        {"trading_date": "2026-09-09", "open": code_base + 0.3, "high": code_base + 0.6,
         "low": code_base + 0.1, "close": code_base + 0.5, "amount": 5e8},
    ]


def _candidate(code: str, score: float, turnover: float = 5.0, prev_close: float = 9.8):
    return {
        "entity_id": code, "decision_id": f"d-{code}", "score": score,
        "turnover_rate": turnover, "decision_date": "2026-09-07",
        "observed_at": "2026-09-07T20:00:00+08:00", "prev_close": prev_close,
    }


def test_the_registered_experiment_freezes_with_every_binding_it_needs():
    frozen = freeze_experiment(_spec())

    assert frozen["experiment_id"] == "rank_surprise_next_open_paper_v1"
    assert frozen["strategy_id"] == "rank_surprise"
    assert frozen["strategy_rules_sha256"]
    assert frozen["sample_start"] == "2026-09-08"
    assert frozen["hold_sessions"] == 1
    assert frozen["weight_semantics"] == WEIGHT_SEMANTICS == "portfolio_budget_fraction"
    assert frozen["experiment_sha256"] == json_sha256(
        {key: value for key, value in frozen.items() if key != "experiment_sha256"}
    )
    # It is an evidence-collection device, not an approval.
    assert frozen["claims_research_gate_passed"] is False
    assert frozen["research_only"] is True


def test_an_incomplete_or_illegal_experiment_never_freezes():
    with pytest.raises(ExperimentError, match="experiment_spec_incomplete"):
        freeze_experiment({"experiment_id": "x"})
    with pytest.raises(ExperimentError, match="unknown_entry_point"):
        freeze_experiment(_spec(entry_point="just_run_it"))
    with pytest.raises(ExperimentError, match="same-session exit"):
        freeze_experiment(_spec(hold_sessions=0))
    with pytest.raises(ExperimentError, match="ranking_keys_required"):
        freeze_experiment(_spec(ranking={"keys": []}))


def test_an_exploratory_scope_must_be_registered_before_it_may_write():
    frozen = freeze_experiment(_spec())

    assert admit(frozen, exploratory_scopes=_scopes())["allowed"] is True
    denied = admit(frozen, exploratory_scopes=["research:exploratory:something_else"])
    assert denied["allowed"] is False
    assert denied["reason"] == "exploratory_scope_not_registered"


def test_an_exploratory_admission_never_claims_the_research_gate():
    decision = admit(freeze_experiment(_spec()), exploratory_scopes=_scopes())

    assert decision["entry_point"] == ENTRY_POINT_EXPLORATORY
    assert decision["research_gate_passed"] is False


def test_a_tampered_experiment_is_refused_before_anything_runs():
    frozen = {**freeze_experiment(_spec()), "hold_sessions": 9}
    decision = admit(frozen, exploratory_scopes=_scopes())

    assert decision["allowed"] is False
    assert decision["reason"] == "experiment_hash_mismatch"


def test_a_pilot_entry_point_actually_reads_the_registry(monkeypatch):
    frozen = freeze_experiment(_spec(
        entry_point=ENTRY_POINT_PILOT, account_scope="paper:pilot:rank_surprise"
    ))
    monkeypatch.setattr(experiment_module.strategy_registry, "paper_runtime_allowed", lambda *a, **k: False)
    monkeypatch.setattr(experiment_module.strategy_registry, "paper_live_weight", lambda *a, **k: 0.0)
    denied = admit(frozen)
    assert denied["allowed"] is False
    assert denied["reason"] == "paper_runtime_not_permitted"

    monkeypatch.setattr(experiment_module.strategy_registry, "paper_runtime_allowed", lambda *a, **k: True)
    monkeypatch.setattr(experiment_module.strategy_registry, "paper_live_weight", lambda *a, **k: 0.05)
    granted = admit(frozen)
    assert granted["allowed"] is True
    assert granted["weight"] == 0.05
    assert granted["weight_semantics"] == "portfolio_budget_fraction"


def test_pilot_permission_alone_is_not_enough_when_the_weight_is_zero(monkeypatch):
    frozen = freeze_experiment(_spec(
        entry_point=ENTRY_POINT_PILOT, account_scope="paper:pilot:rank_surprise"
    ))
    monkeypatch.setattr(experiment_module.strategy_registry, "paper_runtime_allowed", lambda *a, **k: True)
    monkeypatch.setattr(experiment_module.strategy_registry, "paper_live_weight", lambda *a, **k: 0.0)

    assert admit(frozen)["allowed"] is False


def test_idempotency_keys_carry_the_account_and_experiment_scope():
    first = freeze_experiment(_spec())
    second = freeze_experiment(_spec(
        experiment_id="rank_surprise_next_open_paper_v2", hold_sessions=3,
    ))

    key_one = scope_idempotency_key(first, "paper.trade.filled", "2026-09-08", "600000")
    key_two = scope_idempotency_key(second, "paper.trade.filled", "2026-09-08", "600000")

    assert key_one != key_two
    assert first["account_scope"] in key_one
    assert first["experiment_id"] in key_one
    assert key_one == scope_idempotency_key(first, "paper.trade.filled", "2026-09-08", "600000")


def test_ranking_is_pre_registered_and_ignores_upstream_ordering():
    frozen = freeze_experiment(_spec())
    candidates = [
        _candidate("600003", 10.0, turnover=9.0),
        _candidate("600001", 30.0),
        _candidate("600002", 20.0, prev_close=19.8),
    ]
    ordered = [item["entity_id"] for item in rank_candidates(frozen, candidates)]

    assert ordered == ["600001", "600002", "600003"]
    assert ordered == [
        item["entity_id"] for item in rank_candidates(frozen, list(reversed(candidates)))
    ]


def test_ties_break_on_the_pre_registered_key_not_on_arrival_order():
    frozen = freeze_experiment(_spec())
    tied = [_candidate("600009", 30.0, turnover=4.0), _candidate("600002", 30.0, turnover=4.0)]

    assert [item["entity_id"] for item in rank_candidates(frozen, tied)] == ["600002", "600009"]
    assert [
        item["entity_id"] for item in rank_candidates(frozen, list(reversed(tied)))
    ] == ["600002", "600009"]


def test_candidates_beyond_the_budget_are_rejected_and_kept():
    frozen = freeze_experiment(_spec())
    ranked = rank_candidates(frozen, [
        _candidate("600001", 30.0), _candidate("600002", 20.0), _candidate("600003", 10.0),
    ])
    selection = select_within_budget(frozen, ranked, account_equity=100_000.0)

    assert [item["entity_id"] for item in selection["admitted"]] == ["600001", "600002"]
    assert selection["rejected"][0]["entity_id"] == "600003"
    assert selection["rejected"][0]["reason"] == "max_positions_reached"
    # 10% of the account, capped at 5% per name.
    assert selection["total_notional"] == pytest.approx(10_000.0)
    assert selection["allocated_notional"] == pytest.approx(10_000.0)
    assert all(item["order_amount"] == pytest.approx(5_000.0) for item in selection["admitted"])


def test_the_gold_line_produces_a_real_fill_and_a_recoverable_run_record():
    frozen = freeze_experiment(_spec())
    admission = admit(frozen, exploratory_scopes=_scopes())
    ranked = rank_candidates(frozen, [_candidate("600001", 30.0), _candidate("600002", 20.0, prev_close=19.8)])
    selection = select_within_budget(frozen, ranked, account_equity=100_000.0)
    results = simulate_admitted(
        frozen, selection["admitted"], {"600001": _bars(10.0), "600002": _bars(20.0)}
    )
    run = summarise_run(frozen, admission, selection, results, asof="2026-09-08")

    assert run["status"] == "ok"
    assert run["status_counts"]["exited"] == 2
    assert run["execution_evidence"] is True
    assert run["research_only"] is True
    assert run["live_order_sent"] is False
    assert len(run["realised_net_returns"]) == 2
    for result in results:
        assert result["entry_date"] == "2026-09-08"
        assert result["exit_date"] == "2026-09-09"
        assert result["respects_t_plus_one_from_entry"] is True
    assert run["run_sha256"] == json_sha256(
        {key: value for key, value in run.items() if key != "run_sha256"}
    )


def test_rerunning_the_gold_line_does_not_produce_a_second_fill():
    frozen = freeze_experiment(_spec())
    admission = admit(frozen, exploratory_scopes=_scopes())
    ranked = rank_candidates(frozen, [_candidate("600001", 30.0)])
    selection = select_within_budget(frozen, ranked, account_equity=100_000.0)
    bars = {"600001": _bars(10.0)}

    first = summarise_run(
        frozen, admission, selection,
        simulate_admitted(frozen, selection["admitted"], bars), asof="2026-09-08",
    )
    second = summarise_run(
        frozen, admission, selection,
        simulate_admitted(frozen, selection["admitted"], bars), asof="2026-09-08",
    )

    assert first["run_sha256"] == second["run_sha256"]
    assert first["status_counts"] == second["status_counts"] == {"exited": 1}


def test_an_unfillable_candidate_stays_in_the_denominator():
    frozen = freeze_experiment(_spec())
    admission = admit(frozen, exploratory_scopes=_scopes())
    ranked = rank_candidates(frozen, [_candidate("600001", 30.0)])
    selection = select_within_budget(frozen, ranked, account_equity=100_000.0)
    sealed = [
        {"trading_date": "2026-09-08", "open": 10.78, "high": 10.78,
         "low": 10.78, "close": 10.78, "amount": 5e8},
        {"trading_date": "2026-09-09", "open": 10.9, "high": 11.0,
         "low": 10.7, "close": 10.8, "amount": 5e8},
    ]
    run = summarise_run(
        frozen, admission, selection,
        simulate_admitted(frozen, selection["admitted"], {"600001": sealed}),
        asof="2026-09-08",
    )

    assert run["unfilled"] == 1
    assert run["considered"] == 1
    assert run["realised_net_returns"] == []
    assert run["execution_evidence"] is False


def test_no_candidates_reports_no_eligible_evidence_rather_than_an_empty_success():
    frozen = freeze_experiment(_spec())
    admission = admit(frozen, exploratory_scopes=_scopes())
    selection = select_within_budget(frozen, [], account_equity=100_000.0)
    run = summarise_run(frozen, admission, selection, [], asof="2026-09-08")

    assert run["status"] == "no_eligible_evidence"
    assert run["execution_evidence"] is False


def test_the_registered_catalog_matches_the_module_contract():
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    assert catalog["schema"] == "exploratory_paper_experiment_catalog_v1"
    for spec in catalog["experiments"]:
        frozen = freeze_experiment(spec)
        assert frozen["entry_point"] in experiment_module.ENTRY_POINTS
        if frozen["entry_point"] == ENTRY_POINT_EXPLORATORY:
            assert frozen["account_scope"] in catalog["registered_exploratory_scopes"]
