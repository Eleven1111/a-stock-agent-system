import json

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
