import portfolio_policy


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
