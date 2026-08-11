import portfolio_policy


EVIDENCE = {
    "schema": "portfolio_risk_evidence_v2",
    "asof": "2026-07-10",
    "source": "risk-engine-fixture",
    "data_cutoff": "2026-07-09",
    "proposed_position_pct": 5.0,
    "coverage": 1.0,
    "correlation": 0.35,
    "beta": 1.05,
    "style_exposure_pct": 22.0,
    "adv_participation_pct": 3.0,
    "portfolio_volatility_pct": 18.0,
}
LIMITS = {
    "max_correlation": 0.8,
    "max_beta": 1.3,
    "max_style_exposure_pct": 40.0,
    "max_adv_participation_pct": 10.0,
    "max_portfolio_volatility_pct": 25.0,
    "min_coverage": 0.95,
    "max_age_days": 1,
}


def test_complete_fresh_factor_evidence_passes():
    result = portfolio_policy.evaluate_factor_liquidity_risk(
        EVIDENCE, limits=LIMITS, decision_asof="2026-07-10"
    )
    assert result["allowed"] is True
    assert result["status"] == "passed"
    assert result["reasons"] == []


def test_missing_or_stale_factor_evidence_fails_closed():
    missing = dict(EVIDENCE)
    missing.pop("beta")
    missing_result = portfolio_policy.evaluate_factor_liquidity_risk(
        missing, limits=LIMITS, decision_asof="2026-07-10"
    )
    assert "beta_missing" in missing_result["reasons"]
    assert missing_result["status"] == "blocked"
    stale = dict(EVIDENCE, asof="2026-07-01")
    stale_result = portfolio_policy.evaluate_factor_liquidity_risk(
        stale, limits=LIMITS, decision_asof="2026-07-10"
    )
    assert "risk_evidence_stale" in stale_result["reasons"]
    assert stale_result["status"] == "blocked"


def test_each_factor_limit_has_stable_rejection_reason():
    cases = {
        "correlation": (0.81, "correlation_limit"),
        "beta": (1.31, "beta_limit"),
        "style_exposure_pct": (40.1, "style_limit"),
        "adv_participation_pct": (10.1, "adv_limit"),
        "portfolio_volatility_pct": (25.1, "volatility_limit"),
    }
    for field, (value, reason) in cases.items():
        result = portfolio_policy.evaluate_factor_liquidity_risk(
            dict(EVIDENCE, **{field: value}),
            limits=LIMITS,
            decision_asof="2026-07-10",
        )
        assert reason in result["reasons"]
        assert result["allowed"] is False
        assert result["status"] == "rejected"


def test_complete_live_admission_fails_closed_without_factor_evidence():
    portfolio = {"cash": 100_000, "positions": []}
    result = portfolio_policy.evaluate_complete_admission(
        portfolio,
        {"code": "600519", "sector": "白酒"},
        5.0,
        factor_evidence=None,
        decision_asof="2026-07-10",
    )
    assert result["allowed"] is False
    assert result["status"] == "blocked"
    assert "risk_evidence_schema_invalid" in result["reasons"]

    passed = portfolio_policy.evaluate_complete_admission(
        portfolio,
        {"code": "600519", "sector": "白酒"},
        5.0,
        factor_evidence=EVIDENCE,
        decision_asof="2026-07-10",
    )
    assert passed["allowed"] is True
    assert passed["status"] == "passed"


def test_position_larger_than_precomputed_evidence_is_blocked():
    result = portfolio_policy.evaluate_complete_admission(
        {"cash": 100_000, "positions": []},
        {"code": "600519", "sector": "白酒"},
        6.0,
        factor_evidence={**EVIDENCE, "proposed_position_pct": 4.0},
        decision_asof="2026-07-10",
    )

    assert result["status"] == "blocked"
    assert "risk_evidence_position_understated" in result["reasons"]


def test_friday_data_cutoff_is_fresh_on_monday_but_older_cutoff_is_blocked():
    monday = dict(EVIDENCE, asof="2026-07-13", data_cutoff="2026-07-10")
    fresh = portfolio_policy.evaluate_factor_liquidity_risk(
        monday, limits=LIMITS, decision_asof="2026-07-13"
    )
    stale = portfolio_policy.evaluate_factor_liquidity_risk(
        {**monday, "data_cutoff": "2026-07-09"},
        limits=LIMITS,
        decision_asof="2026-07-13",
    )

    assert fresh["status"] == "passed"
    assert stale["status"] == "blocked"
    assert "risk_evidence_data_stale" in stale["reasons"]


def test_non_finite_factor_values_fail_closed():
    result = portfolio_policy.evaluate_factor_liquidity_risk(
        {**EVIDENCE, **{field: float("nan") for field in portfolio_policy._FACTOR_LIMITS}},
        limits=LIMITS,
        decision_asof="2026-07-10",
    )

    assert result["status"] == "blocked"
    assert result["allowed"] is False
    assert all(value is None for value in result["measured"].values())


def test_non_finite_factor_limits_fail_closed():
    result = portfolio_policy.evaluate_factor_liquidity_risk(
        EVIDENCE,
        limits={**LIMITS, "max_beta": float("inf"), "min_coverage": float("nan")},
        decision_asof="2026-07-10",
    )

    assert result["status"] == "blocked"
    assert "max_beta_invalid" in result["reasons"]
    assert "min_coverage_invalid" in result["reasons"]


def test_missing_or_invalid_position_evidence_fails_closed():
    missing = dict(EVIDENCE)
    missing.pop("proposed_position_pct")
    missing_result = portfolio_policy.evaluate_complete_admission(
        {"cash": 100_000, "positions": []},
        {"code": "600519", "sector": "白酒"},
        5.0,
        factor_evidence=missing,
        decision_asof="2026-07-10",
    )
    invalid_result = portfolio_policy.evaluate_complete_admission(
        {"cash": 100_000, "positions": []},
        {"code": "600519", "sector": "白酒"},
        float("nan"),
        factor_evidence=EVIDENCE,
        decision_asof="2026-07-10",
    )

    assert "risk_evidence_position_missing" in missing_result["reasons"]
    assert "requested_position_invalid" in invalid_result["reasons"]
    assert missing_result["status"] == invalid_result["status"] == "blocked"
