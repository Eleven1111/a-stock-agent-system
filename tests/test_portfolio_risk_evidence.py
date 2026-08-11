from __future__ import annotations

import portfolio_risk_evidence
from datetime import date, timedelta


def _bars(code_bias: float = 0.0, *, count: int = 45, start: date = date(2026, 5, 1)):
    price = 10.0 + code_bias
    rows = []
    for index in range(count):
        price *= 1.0 + (0.012 if index % 2 == 0 else -0.006)
        rows.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "close": round(price, 4),
            "volume": 100_000 + index * 1_000,
        })
    return rows


def test_build_evidence_uses_only_prior_day_history_and_populates_all_metrics():
    candidate = {"code": "600001", "sector": "半导体"}
    portfolio = {"cash": 100_000, "positions": []}

    evidence = portfolio_risk_evidence.build_candidate_evidence(
        candidate,
        portfolio,
        bars_by_code={"600001": _bars()},
        benchmark_bars=_bars(5.0),
        proposed_position_pct=5.0,
        decision_asof="2026-07-20",
    )

    assert evidence["schema"] == "portfolio_risk_evidence_v2"
    assert evidence["asof"] == "2026-07-20"
    assert evidence["data_cutoff"] < evidence["asof"]
    assert evidence["coverage"] == 1.0
    assert evidence["correlation"] == 0.0
    assert evidence["beta"] is not None
    assert evidence["style_exposure_pct"] == 5.0
    assert evidence["adv_participation_pct"] is not None
    assert evidence["portfolio_volatility_pct"] is not None


def test_missing_history_is_explicitly_incomplete_not_neutral():
    evidence = portfolio_risk_evidence.build_candidate_evidence(
        {"code": "600001", "sector": "半导体"},
        {"cash": 100_000, "positions": []},
        bars_by_code={"600001": []},
        benchmark_bars=[],
        proposed_position_pct=5.0,
        decision_asof="2026-07-20",
    )

    assert evidence["coverage"] < 0.95
    assert evidence["beta"] is None
    assert evidence["adv_participation_pct"] is None
    assert "candidate_history_missing" in evidence["missing_reasons"]


def test_candidate_bundle_is_keyed_by_normalized_stock_code():
    result = portfolio_risk_evidence.build_evidence_bundle(
        [{"code": "sh600001", "sector": "半导体"}],
        {"cash": 100_000, "positions": []},
        bars_by_code={"600001": _bars()},
        benchmark_bars=_bars(5.0),
        proposed_position_pct=5.0,
        decision_asof="2026-07-20",
    )

    assert result["schema"] == "portfolio_risk_evidence_batch_v1"
    assert list(result["evidence_by_code"]) == ["600001"]


def test_oldest_required_holding_controls_data_cutoff():
    evidence = portfolio_risk_evidence.build_candidate_evidence(
        {"code": "600001", "sector": "半导体"},
        {
            "cash": 100_000,
            "positions": [{
                "code": "000002", "sector": "地产", "market_value": 100_000,
            }],
        },
        bars_by_code={
            "600001": _bars(start=date(2026, 6, 4)),
            "000002": _bars(start=date(2026, 5, 1)),
        },
        benchmark_bars=_bars(5.0, start=date(2026, 6, 4)),
        proposed_position_pct=5.0,
        decision_asof="2026-07-20",
    )

    assert evidence["data_cutoff"] == "2026-06-14"


def test_non_finite_daily_bars_do_not_enter_evidence():
    evidence = portfolio_risk_evidence.build_candidate_evidence(
        {"code": "600001", "sector": "半导体"},
        {"cash": 100_000, "positions": []},
        bars_by_code={"600001": [{"date": "2026-07-18", "close": float("nan")}]},
        benchmark_bars=_bars(5.0),
        proposed_position_pct=5.0,
        decision_asof="2026-07-20",
    )

    assert evidence["coverage"] < 0.95
    assert "candidate_history_missing" in evidence["missing_reasons"]
