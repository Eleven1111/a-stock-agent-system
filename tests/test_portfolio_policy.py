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
