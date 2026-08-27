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
    assert artifact["canonical_forward"] is True
    assert artifact["exploratory_reconstruction"] is False
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
