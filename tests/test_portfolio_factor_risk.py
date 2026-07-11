import portfolio_policy


EVIDENCE = {
    "schema": "portfolio_risk_evidence_v1",
    "asof": "2026-07-10",
    "source": "risk-engine-fixture",
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
    assert result["reasons"] == []


def test_missing_or_stale_factor_evidence_fails_closed():
    missing = dict(EVIDENCE)
    missing.pop("beta")
    assert "beta_missing" in portfolio_policy.evaluate_factor_liquidity_risk(
        missing, limits=LIMITS, decision_asof="2026-07-10"
    )["reasons"]
    stale = dict(EVIDENCE, asof="2026-07-01")
    assert "risk_evidence_stale" in portfolio_policy.evaluate_factor_liquidity_risk(
        stale, limits=LIMITS, decision_asof="2026-07-10"
    )["reasons"]


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
    assert "risk_evidence_schema_invalid" in result["reasons"]

    passed = portfolio_policy.evaluate_complete_admission(
        portfolio,
        {"code": "600519", "sector": "白酒"},
        5.0,
        factor_evidence=EVIDENCE,
        decision_asof="2026-07-10",
    )
    assert passed["allowed"] is True
