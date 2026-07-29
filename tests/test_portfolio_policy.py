import portfolio_policy


def test_portfolio_value_uses_runtime_cash_and_positions_not_static_config():
    portfolio = {
        "cash": 12000,
        "positions": [{"code": "600001", "shares": 800, "current_price": 10.0}],
    }

    assert portfolio_policy.portfolio_value(portfolio) == 20000


def test_missing_runtime_portfolio_fails_closed():
    result = portfolio_policy.evaluate_new_position(
        {},
        code="600001",
        sector="半导体",
        proposed_position_pct=5,
        max_single_position_pct=25,
        max_sector_exposure_pct=40,
    )

    assert result["allowed"] is False
    assert result["reasons"] == ["portfolio_value_unavailable"]


def test_existing_position_plus_proposal_cannot_exceed_single_name_limit():
    portfolio = {
        "cash": 73000,
        "positions": [
            {
                "code": "600001",
                "sector": "半导体",
                "shares": 2200,
                "current_price": 10.0,
            }
        ],
    }

    result = portfolio_policy.evaluate_new_position(
        portfolio,
        code="600001",
        sector="半导体",
        proposed_position_pct=5.0,
        max_single_position_pct=25.0,
        max_sector_exposure_pct=40.0,
    )

    assert result["allowed"] is False
    assert "single_position_limit" in result["reasons"]


def test_sector_limit_includes_existing_positions_and_proposal():
    portfolio = {
        "cash": 60000,
        "positions": [
            {
                "code": "600001",
                "sector": "半导体",
                "shares": 3500,
                "current_price": 10.0,
            }
        ],
    }

    result = portfolio_policy.evaluate_new_position(
        portfolio,
        code="600002",
        sector="半导体",
        proposed_position_pct=8.0,
        max_single_position_pct=25.0,
        max_sector_exposure_pct=40.0,
    )

    assert result["allowed"] is False
    assert "sector_exposure_limit" in result["reasons"]


def test_unknown_candidate_sector_fails_closed():
    result = portfolio_policy.evaluate_new_position(
        {"cash": 100000, "positions": []},
        code="600001",
        sector="",
        proposed_position_pct=5.0,
        max_single_position_pct=25.0,
        max_sector_exposure_pct=40.0,
    )

    assert result["allowed"] is False
    assert result["code"] == "UNKNOWN_SECTOR"
    assert "unknown_sector" in result["reasons"]
    assert result["projected_sector_exposure_pct"] is None


def test_existing_unknown_sector_makes_concentration_unverifiable():
    portfolio = {
        "cash": 80000,
        "positions": [{"code": "600001", "shares": 2000, "current_price": 10.0}],
    }

    result = portfolio_policy.evaluate_new_position(
        portfolio,
        code="600002",
        sector="半导体",
        proposed_position_pct=5.0,
        max_single_position_pct=25.0,
        max_sector_exposure_pct=40.0,
    )

    assert result["allowed"] is False
    assert "existing_position_sector_unknown" in result["reasons"]
    assert result["unknown_sector_codes"] == ["600001"]


def test_existing_industry_field_is_counted_toward_sector_limit():
    portfolio = {
        "cash": 60000,
        "positions": [
            {
                "code": "600001",
                "industry": "半导体",
                "industry_source": "candidate_snapshot",
                "industry_asof": "2026-07-10",
                "shares": 3500,
                "current_price": 10.0,
            }
        ],
    }

    result = portfolio_policy.evaluate_new_position(
        portfolio,
        code="600002",
        sector="半导体",
        proposed_position_pct=8.0,
        max_single_position_pct=25.0,
        max_sector_exposure_pct=40.0,
    )

    assert result["allowed"] is False
    assert "sector_exposure_limit" in result["reasons"]
    assert result["projected_sector_exposure_pct"] > 40.0


def _research_signal(
    code,
    strategy_id,
    priority,
    *,
    sector="半导体",
    proposed_position_pct=10.0,
    requested_capacity=100_000.0,
):
    return {
        "code": code,
        "strategy_id": strategy_id,
        "priority": priority,
        "sector": sector,
        "proposed_position_pct": proposed_position_pct,
        "requested_capacity": requested_capacity,
        "signal_id": f"{strategy_id}:{code}",
    }


def test_research_coordinator_deduplicates_security_and_preserves_attribution():
    result = portfolio_policy.coordinate_research_allocations(
        [
            _research_signal("600001", "tail_close", 90),
            _research_signal("600001", "trend_pullback", 80),
            _research_signal("600002", "trend_pullback", 70),
        ],
        security_capacity={"600001": 100_000, "600002": 100_000},
        max_single_pct=15,
        max_sector_pct=40,
    )

    assert [item["code"] for item in result["allocations"]] == [
        "600001",
        "600002",
    ]
    assert result["allocations"][0]["strategy_id"] == "tail_close"
    assert result["allocations"][0]["attribution"] == {
        "primary_strategy_id": "tail_close",
        "contributing_strategy_ids": ["tail_close", "trend_pullback"],
        "standalone_strategy_ids": ["tail_close", "trend_pullback"],
        "incremental_strategy_id": "tail_close",
    }
    assert result["allocations"][0]["allocated_capacity"] == 100_000
    assert any(
        item["reason"] == "duplicate_security"
        and item["strategy_id"] == "trend_pullback"
        for item in result["rejections"]
    )
    assert result["standalone_count"] == 3
    assert result["incremental_count"] == 2
    assert result["standalone_by_strategy"] == {
        "tail_close": 1,
        "trend_pullback": 2,
    }
    assert result["incremental_by_strategy"] == {
        "tail_close": 1,
        "trend_pullback": 1,
    }


def test_research_coordinator_shares_capacity_by_deterministic_priority():
    signals = [
        _research_signal("600003", "strategy_b", 80, sector="消费"),
        _research_signal("600002", "strategy_a", 80, sector="医药"),
        _research_signal("600001", "strategy_z", 90, sector="半导体"),
    ]

    forward = portfolio_policy.coordinate_research_allocations(
        signals,
        security_capacity={
            "600001": 100_000,
            "600002": 50_000,
            "600003": 0,
        },
        max_single_pct=15,
        max_sector_pct=40,
    )
    reversed_input = portfolio_policy.coordinate_research_allocations(
        list(reversed(signals)),
        security_capacity={
            "600003": 0,
            "600002": 50_000,
            "600001": 100_000,
        },
        max_single_pct=15,
        max_sector_pct=40,
    )

    assert forward == reversed_input
    assert [item["code"] for item in forward["allocations"]] == [
        "600001",
        "600002",
    ]
    assert forward["allocations"][1]["allocated_capacity"] == 50_000
    assert forward["allocations"][1]["allocated_position_pct"] == 5.0
    assert forward["shared_capacity"]["allocated_by_code"] == {
        "600001": 100_000.0,
        "600002": 50_000.0,
        "600003": 0.0,
    }
    assert forward["shared_capacity"]["remaining_by_code"] == {
        "600001": 0.0,
        "600002": 0.0,
        "600003": 0.0,
    }
    assert any(
        item["code"] == "600003" and item["reason"] == "capacity_exhausted"
        for item in forward["rejections"]
    )


def test_research_coordinator_enforces_single_and_sector_limits():
    result = portfolio_policy.coordinate_research_allocations(
        [
            _research_signal(
                "600001",
                "oversized",
                100,
                proposed_position_pct=16,
            ),
            _research_signal("600002", "first", 90, proposed_position_pct=12),
            _research_signal("600003", "second", 80, proposed_position_pct=12),
            _research_signal(
                "600004",
                "diversified",
                70,
                sector="医药",
                proposed_position_pct=10,
            ),
        ],
        security_capacity={
            "600001": 100_000,
            "600002": 100_000,
            "600003": 100_000,
            "600004": 100_000,
        },
        max_single_pct=15,
        max_sector_pct=20,
    )

    assert [item["code"] for item in result["allocations"]] == [
        "600001",
        "600002",
        "600004",
    ]
    reasons = {item["code"]: item["reason"] for item in result["rejections"]}
    assert reasons["600003"] == "sector_exposure_limit"
    assert result["allocations"][0]["allocated_capacity"] == 93_750
    assert result["allocations"][0]["allocated_position_pct"] == 15
    assert result["allocations"][0]["limited_by"] == [
        "single_position_limit"
    ]
    assert round(result["allocations"][1]["allocated_position_pct"], 4) == 5
    assert result["allocations"][1]["limited_by"] == [
        "sector_exposure_limit"
    ]
    assert result["shared_capacity"]["sector_allocated_pct"] == {
        "医药": 10.0,
        "半导体": 20.0,
    }


def test_research_coordinator_fails_closed_on_unattributable_signal():
    result = portfolio_policy.coordinate_research_allocations(
        [
            _research_signal("600001", "", 90),
            _research_signal("600002", "tail_close", 80, sector=""),
        ],
        security_capacity={"600001": 100_000, "600002": 100_000},
        max_single_pct=15,
        max_sector_pct=40,
    )

    assert result["allocations"] == []
    assert {item["reason"] for item in result["rejections"]} == {
        "strategy_id_missing",
        "unknown_sector",
    }


def test_research_coordinator_accepts_requested_notional_alias():
    result = portfolio_policy.coordinate_research_allocations(
        [{
            "code": "600001",
            "strategy_id": "tail_close",
            "priority": 90,
            "sector_id": "半导体",
            "proposed_position_pct": 10,
            "requested_notional": 80_000,
        }],
        security_capacity={"600001": 100_000},
        max_single_pct=15,
        max_sector_pct=40,
    )

    assert result["allocations"][0]["requested_capacity"] == 80_000
    assert result["allocations"][0]["allocated_capacity"] == 80_000
    assert result["shared_capacity"]["remaining_by_code"]["600001"] == 20_000
