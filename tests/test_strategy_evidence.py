"""Unified point-in-time evidence for the six non-live strategies."""

import pytest

import strategy_evidence as evidence


def _candidate(code="600001", **overrides):
    row = {
        "code": code,
        "sector": "通信设备",
        "change_pct": 10.0,
        "board_height": 2,
        "market_space_height": 3,
        "first_seal": "09:35",
        "price": 11.0,
        "turnover": 10.0,
        "volume": 100_000,
    }
    row.update(overrides)
    return row


def _bars(code="600001"):
    rows = []
    for day in range(1, 22):
        rows.append({
            "code": code,
            "trading_date": f"2026-08-{day:02d}",
            "close": 9.0 + day / 10,
            "high": 9.2 + day / 10,
            "low": 8.8 + day / 10,
            "volume": 1000 + day,
            "pct_chg": float(day),
            "turn": 3.0,
        })
    return rows


def _minutes():
    rows = []
    cumulative = 0
    for offset in range(31):
        cumulative += 10 + offset
        minute = 9 * 60 + 30 + offset
        rows.append({
            "time": f"{minute // 60:02d}{minute % 60:02d}",
            "price": 10.0 + (0.01 if offset % 2 else 0.0),
            "cum_volume": cumulative,
            "cum_amount": cumulative * 100,
        })
    return rows


def test_cohort_is_deduplicated_union_and_never_silently_truncated():
    candidates = [_candidate(), _candidate("600002", board_height=0, first_seal=None)]
    pool = [{"代码": "600001"}]
    auction = {"shortlist": [{"code": "600002"}]}

    assert evidence.select_cohort(candidates, auction, pool, tracked_codes=["600001"]) == [
        "600001", "600002"
    ]
    with pytest.raises(evidence.EvidenceBudgetExceeded, match="2>1"):
        evidence.select_cohort(candidates, auction, pool, max_codes=1)


def test_auction_factor_and_rejected_universe_does_not_expand_minute_cohort():
    auction = {
        "shortlist": [{"code": "600001"}],
        "factors": [{"code": f"{index:06d}"} for index in range(1, 500)],
        "rejected": [{"code": f"{index:06d}"} for index in range(500, 1000)],
    }
    assert evidence.select_cohort([], auction, []) == ["600001"]


def test_s1_prefilter_expands_only_candidates_that_can_pass_both_rank_conditions():
    candidates = [
        _candidate(f"60000{index}", sector="通信", board_height=None)
        for index in range(1, 7)
    ]
    auction = {"factors": [
        {"code": f"60000{index}", "auction_gap_pct": float(7 - index)}
        for index in range(1, 7)
    ]}
    bars = [{
        "code": f"60000{index}", "trading_date": "2026-08-21",
        "pct_chg": float(index), "close": 10, "high": 10, "volume": 1000,
    } for index in range(1, 7)]
    assert evidence.rank_surprise_targets(
        "2026-08-22", candidates, auction, bars
    ) == ["600001", "600002"]


def test_build_maps_only_observed_sources_and_records_provenance():
    artifact = evidence.build_evidence(
        "2026-08-22",
        candidates=[_candidate()],
        auction={"asof": "2026-08-22", "factors": [
            {"code": "600001", "auction_gap_pct": 3.2}
        ]},
        selection={"asof": "2026-08-22", "market_state": {
            "available": True, "dominant_state": "S2", "deteriorating": False
        }},
        limitup_rows=[{
            "代码": "600001", "所属行业": "通信设备", "首次封板时间": 93500,
            "最后封板时间": 100000, "炸板次数": 1, "流通市值": 1_100_000_000,
        }],
        minute_rows={"600001": _minutes()},
        daily_bars=_bars(),
        sentiment_series=[],
    )

    row = artifact["records"][0]
    assert row["auction_strength"] == 3.2
    assert row["prior_return_pct"] == 21.0  # strictly before D0
    assert row["reseal_time"] == "100000"
    assert row["pre_reseal_turnover_pct"] is not None
    assert row["volume_ratio"] is not None
    assert row["volume_ratio_source"] == "tencent_minute_intraday:09:45"
    assert row["breakout_time"] is None  # first seal is not silently relabelled
    assert row["evidence_provenance"]["reseal_time"]["source"] == "eastmoney_zt_pool"
    assert row["evidence_provenance"]["reseal_time"]["observed_at"] == "2026-08-22T15:00:00+08:00"
    assert row["evidence_provenance"]["reseal_time"]["source_identity"] == "eastmoney_zt_pool"
    # 日线的全天 turn 不是「过去 20 日相同回封时刻累计换手」；绝不能代替。
    assert row["turnover_baseline_median_pct"] is None
    assert row["turnover_baseline_sample_days"] is None
    assert row["turnover_baseline_semantics"] == "unavailable"
    assert artifact["evidence_qualification"]["divergence_reseal"]["class"] == "unavailable"
    assert artifact["research_only"] is True
    assert artifact["execution_eligible"] is False


def test_missing_source_stays_unavailable_in_coverage_not_a_negative_observation():
    artifact = evidence.build_evidence(
        "2026-08-22",
        candidates=[_candidate()],
        auction={"asof": "2026-08-22", "shortlist": [{"code": "600001"}]},
        selection={"asof": "2026-08-22"},
        limitup_rows=[{"代码": "600001", "所属行业": "通信设备"}],
        minute_rows={},
        daily_bars=[],
        sentiment_series=[],
    )

    row = artifact["records"][0]
    assert row["volume_ratio"] is None
    assert "volume_ratio" in artifact["coverage"]["rank_surprise"]["source_missing_fields"]
    assert artifact["coverage"]["rank_surprise"]["ready_records"] == 0


def test_reconstructed_sentiment_is_strategy_scoped_exploratory_not_dataset_canonical():
    sentiment = [{
        "trading_date": f"2026-01-{day:02d}",
        "observed_at": f"2026-01-{day:02d}T15:00:00+08:00",
        "source": "local_history_cache",
        "status": "ok",
    } for day in range(1, 21)]
    artifact = evidence.build_evidence(
        "2026-08-22", candidates=[_candidate()], auction={"asof": "2026-08-22"},
        selection={"asof": "2026-08-22"},
        limitup_rows=[{"代码": "600001", "所属行业": "通信设备"}],
        minute_rows={}, daily_bars=_bars(), sentiment_series=sentiment,
    )

    assert artifact["canonical_forward"] is False
    assert artifact["evidence_class"] == "mixed"
    assert artifact["evidence_qualification"]["ice_point_reversal"]["class"] == "exploratory_reconstruction"
    assert artifact["evidence_qualification"]["ice_point_reversal"]["canonical_forward_eligible"] is False
    # Qualification is derived from source identity, not a caller-provided boolean.
    assert "canonical_forward" not in sentiment[0]


def test_s6_preserves_the_confirmed_security_binding_instead_of_market_proxy(monkeypatch):
    def scored(rows, *_args, **_kwargs):
        output = []
        for raw in rows:
            row = dict(raw)
            row["leader_score_shadow"] = {"status": "ok", "score": 88.0}
            output.append(row)
        return output

    monkeypatch.setattr(evidence.hot_money_selection, "apply_leader_score_shadow", scored)
    artifact = evidence.build_evidence(
        "2026-08-22",
        candidates=[_candidate()],
        auction={"asof": "2026-08-22", "shortlist": [{"code": "600001"}]},
        selection={"asof": "2026-08-22"},
        limitup_rows=[{
            "代码": "600001", "所属行业": "通信设备", "首次封板时间": "09:35",
        }],
        minute_rows={}, daily_bars=_bars(), sentiment_series=[],
    )

    rows = artifact["strategy_records"]["ice_point_reversal"]
    assert len(rows) == 1
    assert rows[0]["code"] == "600001"
    assert rows[0]["ice_point_leader_candidate"] is True
    assert rows[0]["ice_point_leader_binding"] == {
        "code": "600001",
        "leader_score_shadow": 88.0,
        "leader_score_threshold": 80,
        "leader_confirmed": True,
        "confirmation_source": "eastmoney_zt_pool",
        "confirmation_time": "000935",
    }
    assert artifact["market_state"]["tradeable_leader_bindings"] == [
        rows[0]["ice_point_leader_binding"]
    ]


def test_s6_without_qualified_leader_is_unavailable_and_has_no_market_row(monkeypatch):
    def weak_score(rows, *_args, **_kwargs):
        return [{**row, "leader_score_shadow": {"status": "ok", "score": 79.9}}
                for row in rows]

    monkeypatch.setattr(evidence.hot_money_selection, "apply_leader_score_shadow", weak_score)
    artifact = evidence.build_evidence(
        "2026-08-22", candidates=[_candidate()],
        auction={"asof": "2026-08-22", "shortlist": [{"code": "600001"}]},
        selection={"asof": "2026-08-22"},
        limitup_rows=[{"代码": "600001", "首次封板时间": "09:35"}],
        minute_rows={}, daily_bars=_bars(), sentiment_series=[],
    )
    assert artifact["strategy_records"]["ice_point_reversal"] == []
    assert artifact["market_state"]["tradeable_leader_bindings"] == []
    coverage = artifact["coverage"]["ice_point_reversal"]
    assert coverage["ready_records"] == 0
    assert "market_state.tradeable_leader_binding" in coverage["missing_fields"]


def test_asof_mismatch_fails_closed():
    with pytest.raises(ValueError, match="auction asof mismatch"):
        evidence.build_evidence(
            "2026-08-22",
            candidates=[_candidate()],
            auction={"asof": "2026-08-21", "shortlist": [{"code": "600001"}]},
            selection={},
            limitup_rows=[{"代码": "600001"}],
            minute_rows={},
            daily_bars=[],
            sentiment_series=[],
        )
