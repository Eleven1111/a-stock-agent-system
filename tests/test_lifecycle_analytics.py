import json
import pytest

import lifecycle_analytics as la


def _rec(code, *, stage_events, current_stage, outcome=None, **fields):
    return {
        "code": code,
        "current_stage": current_stage,
        "stage_history": [
            {"stage": s, "selected": sel} for s, sel in stage_events
        ],
        "outcome": outcome or {"resolved": False},
        **fields,
    }


def test_passed_stages_only_counts_selected_events():
    watch = _rec("600000", stage_events=[("discovery", True)], current_stage="watch_pool")
    rejected = _rec("600001", stage_events=[("discovery", False)], current_stage="discovery_rejected")

    assert la.passed_stages(watch) == {"discovery"}
    assert la.passed_stages(rejected) == set()


def test_rejected_at_stage_reads_current_stage():
    assert la.rejected_at_stage({"current_stage": "discovery_rejected"}) == "discovery"
    assert la.rejected_at_stage({"current_stage": "rejected:auction_shortlist"}) == "auction_shortlist"
    assert la.rejected_at_stage({"current_stage": "watch_pool"}) is None


def test_spearman_ic_detects_perfect_and_inverse_rank():
    perfect = la.spearman_ic([(1, 10), (2, 20), (3, 30), (4, 40)])
    inverse = la.spearman_ic([(1, 40), (2, 30), (3, 20), (4, 10)])
    assert perfect["ic"] == 1.0
    assert inverse["ic"] == -1.0
    assert perfect["n"] == 4


def test_spearman_ic_flags_insufficient_sample():
    result = la.spearman_ic([(1, 2), (2, 3)])
    assert result["ic"] is None
    assert result["note"] == "insufficient_sample"


def test_quantile_buckets_are_monotonic_for_predictive_score():
    records = [
        {"daban_score": i, "outcome": {"resolved": True, "t3_close_ret": i * 1.0}}
        for i in range(20)
    ]
    buckets = la.quantile_buckets(records, "daban_score", "t3_close_ret", n_buckets=4)
    means = [b["mean_outcome"] for b in buckets]
    assert len(buckets) == 4
    assert means == sorted(means)  # increasing score -> increasing outcome


def test_funnel_analysis_multi_stage_recall_and_regret():
    # Build a 3-gate funnel: discovery -> auction_shortlist -> open_confirmed.
    records = []
    # A big mover rejected at discovery (the regret case).
    records.append(_rec(
        "000001", stage_events=[("discovery", False)],
        current_stage="discovery_rejected",
        outcome={"resolved": True, "t3_close_ret": 15.0},
    ))
    # A big mover that passed discovery but was cut at auction_shortlist.
    records.append(_rec(
        "000002", stage_events=[("discovery", True), ("auction_shortlist", False)],
        current_stage="rejected:auction_shortlist",
        outcome={"resolved": True, "t3_close_ret": 12.0},
    ))
    # A big mover that made it all the way through.
    records.append(_rec(
        "000003",
        stage_events=[("discovery", True), ("auction_shortlist", True), ("open_confirmed", True)],
        current_stage="open_confirmed",
        outcome={"resolved": True, "t3_close_ret": 11.0},
    ))
    # A dud that passed discovery (dilutes the pool, not a big mover).
    records.append(_rec(
        "000004", stage_events=[("discovery", True), ("auction_shortlist", False)],
        current_stage="rejected:auction_shortlist",
        outcome={"resolved": True, "t3_close_ret": -3.0},
    ))

    result = la.funnel_analysis(records, outcome_key="t3_close_ret", big_mover_threshold=9.9)
    gates = {g["stage"]: g for g in result["gates"]}

    # Discovery: 3 big movers entered, 2 passed -> recall 2/3; 1 big mover wrongly rejected.
    disc = gates["discovery"]
    assert disc["entered"] == 4
    assert disc["passed"] == 3
    assert disc["big_movers_entered"] == 3
    assert disc["big_movers_passed"] == 2
    assert disc["recall"] == round(2 / 3, 4)
    assert disc["big_movers_wrongly_rejected"] == 1

    # Auction shortlist: entrants are the 3 that passed discovery.
    auc = gates["auction_shortlist"]
    assert auc["entered"] == 3
    assert auc["passed"] == 1
    assert auc["big_movers_wrongly_rejected"] == 1  # 000002


def test_ordered_stages_sorts_known_then_unknown():
    records = [
        _rec("1", stage_events=[("discovery", True), ("mystery_stage", True)], current_stage="mystery_stage"),
        _rec("2", stage_events=[("discovery", True), ("open_confirmed", True)], current_stage="open_confirmed"),
    ]
    stages = la.ordered_stages(records)
    assert stages.index("discovery") < stages.index("open_confirmed")
    assert stages[-1] == "mystery_stage"  # unknown sorts last


def test_recall_source_defaults_to_full_market_enumeration_when_untagged():
    assert la.recall_source({"code": "600000"}) == "full_market_enumeration"
    assert la.recall_source({"code": "600000", "recall_source": ""}) == "full_market_enumeration"
    assert la.recall_source({"code": "300777", "recall_source": "nl_screening_eastmoney"}) == (
        "nl_screening_eastmoney"
    )


def test_recall_source_breakdown_separates_channels_and_computes_stats():
    records = [
        _rec(
            "600000", stage_events=[("discovery", True)], current_stage="watch_pool",
            outcome={"resolved": True, "t3_close_ret": 12.0},
            recall_source="full_market_enumeration",
        ),
        _rec(
            "600001", stage_events=[("discovery", False)], current_stage="discovery_rejected",
            outcome={"resolved": True, "t3_close_ret": -2.0},
            recall_source="full_market_enumeration",
        ),
        _rec(
            "300777", stage_events=[("discovery", True)], current_stage="watch_pool",
            outcome={"resolved": True, "t3_close_ret": 15.0},
            recall_source="nl_screening_eastmoney",
        ),
    ]

    breakdown = la.recall_source_breakdown(records, outcome_key="t3_close_ret", big_mover_threshold=9.9)
    sources = breakdown["sources"]

    assert set(sources) == {"full_market_enumeration", "nl_screening_eastmoney"}
    assert sources["full_market_enumeration"]["sample_size"] == 2
    assert sources["nl_screening_eastmoney"]["sample_size"] == 1
    assert sources["nl_screening_eastmoney"]["big_movers"] == 1
    assert sources["nl_screening_eastmoney"]["mean_outcome"] == 15.0
    assert sources["full_market_enumeration"]["funnel"]["gates"][0]["stage"] == "discovery"


def test_recall_source_breakdown_untagged_records_group_into_full_market_default():
    records = [
        _rec("600000", stage_events=[("discovery", True)], current_stage="watch_pool",
             outcome={"resolved": True, "t3_close_ret": 1.0}),
    ]
    breakdown = la.recall_source_breakdown(records)
    assert list(breakdown["sources"]) == ["full_market_enumeration"]


def test_load_settled_records_filters_and_tags_day(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    day_dir = tmp_path / "skills" / "stock-triage" / "data" / "candidate_lifecycle"
    day_dir.mkdir(parents=True)
    (day_dir / "2026-06-25.json").write_text(json.dumps({
        "schema": "candidate_lifecycle_v1",
        "records": [
            {"code": "600000", "outcome": {"resolved": True, "t3_close_ret": 5.0}, "stage_history": []},
            {"code": "600001", "outcome": {"resolved": False}, "stage_history": []},
        ],
    }), encoding="utf-8")

    records = la.load_settled_records(["2026-06-25"])
    assert len(records) == 1
    assert records[0]["code"] == "600000"
    assert records[0]["asof"] == "2026-06-25"


def test_reflexivity_ablation_measures_cost_adjusted_risk_reduction():
    records = [
        {
            "code": "600001",
            "reflexivity": {
                "phase": "distribution",
                "defensive_guards": ["leader_isolation_exit_v1"],
                "risk_multiplier": 0.0,
            },
            "outcome": {
                "resolved": True,
                "t3_close_ret": -8.0,
                "max_drawdown": -12.0,
            },
        },
        {
            "code": "600002",
            "reflexivity": {
                "phase": "saturation",
                "defensive_guards": ["algorithmic_false_consensus_guard_v1"],
                "risk_multiplier": 0.5,
            },
            "outcome": {
                "resolved": True,
                "t3_close_ret": -4.0,
                "max_drawdown": -7.0,
            },
        },
        {
            "code": "600003",
            "reflexivity": {
                "phase": "diffusion",
                "defensive_guards": [],
                "risk_multiplier": 1.0,
            },
            "outcome": {
                "resolved": True,
                "t3_close_ret": 6.0,
                "max_drawdown": -2.0,
            },
        },
    ]

    report = la.reflexivity_ablation(
        records, outcome_key="t3_close_ret", round_trip_cost_bps=20
    )

    assert report["status"] == "ok"
    assert report["sample_size"] == 3
    assert report["guarded_count"] == 2
    assert report["baseline"]["mean_net_ret"] == -2.2
    assert report["reflexivity"]["mean_net_ret"] == 1.2
    assert report["delta"]["mean_net_ret"] == 3.4
    assert report["delta"]["worst_case_ret"] > 0
    assert report["guards"]["leader_isolation_exit_v1"]["sample_size"] == 1


def test_reflexivity_ablation_rejects_unresolved_and_unknown_records():
    records = [
        {"code": "1", "reflexivity": {}, "outcome": {"resolved": True, "t3_close_ret": 2}},
        {
            "code": "2",
            "reflexivity": {"phase": "distribution", "risk_multiplier": 0.0},
            "outcome": {"resolved": False, "t3_close_ret": -9},
        },
    ]

    report = la.reflexivity_ablation(records)

    assert report["status"] == "insufficient_data"
    assert report["sample_size"] == 0
    assert report["excluded"]["unknown_reflexivity"] == 1
    assert report["excluded"]["unresolved"] == 1


def test_reflexivity_ablation_merges_policy_guard_from_stage_details():
    records = [{
        "code": "600004",
        "reflexivity": {
            "phase": "saturation",
            "defensive_guards": [],
            "risk_multiplier": 1.0,
        },
        "stage_history": [{
            "stage": "auction_shortlist",
            "selected": True,
            "details": {
                "policy_reasons": ["reflexivity_institution_distribution"],
                "position_multiplier": 0.0,
            },
        }],
        "outcome": {"resolved": True, "t3_close_ret": -5.0},
    }]

    report = la.reflexivity_ablation(records, round_trip_cost_bps=0)

    guard = report["guards"]["institution_distribution_guard_v1"]
    assert report["guarded_count"] == 1
    assert guard["mean_delta"] == 5.0


def test_reflexivity_ablation_rejects_negative_transaction_cost():
    with pytest.raises(ValueError, match="round_trip_cost_bps"):
        la.reflexivity_ablation([], round_trip_cost_bps=-1)


def test_reflexivity_ablation_refuses_to_pool_mixed_config_versions():
    records = [
        {
            "code": "600001",
            "reflexivity": {
                "phase": "diffusion", "risk_multiplier": 1.0,
                "strategy_version": "v1", "config_sha256": "a" * 64,
            },
            "outcome": {"resolved": True, "t3_close_ret": 2.0},
        },
        {
            "code": "600002",
            "reflexivity": {
                "phase": "distribution", "risk_multiplier": 0.0,
                "strategy_version": "v2", "config_sha256": "b" * 64,
            },
            "outcome": {"resolved": True, "t3_close_ret": -2.0},
        },
    ]

    report = la.reflexivity_ablation(records)

    assert report["status"] == "mixed_versions"
    assert report["sample_size"] == 0
    assert set(report["versions"]) == {"a" * 64, "b" * 64}

    filtered = la.reflexivity_ablation(
        records, expected_config_sha256="a" * 64
    )
    assert filtered["status"] == "ok"
    assert filtered["sample_size"] == 1
    assert filtered["excluded"]["config_mismatch"] == 1
