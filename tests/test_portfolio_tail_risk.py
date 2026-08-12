"""组合尾部风险证据 — 历史 VaR/CVaR 与确定性压力情景（纯函数，不触网）"""

import pytest

from portfolio_risk_evidence import (
    MIN_TAIL_OBSERVATIONS,
    STRESS_BENCHMARK_SHOCKS_PCT,
    _historical_var_cvar,
    build_tail_risk_evidence,
)


ASOF = "2026-08-12"


def _bars(returns, *, start_close=100.0, start_day=1):
    """把日收益序列还原成日线；日期从 2026-01-01 起顺延（含足够历史）。"""
    from datetime import date, timedelta

    bars = [{"date": (date(2026, 1, 1) + timedelta(days=start_day)).isoformat(),
             "close": start_close}]
    close = start_close
    for offset, value in enumerate(returns, start=start_day + 1):
        close = close * (1 + value)
        bars.append(
            {
                "date": (date(2026, 1, 1) + timedelta(days=offset)).isoformat(),
                "close": round(close, 6),
            }
        )
    return bars


def _portfolio():
    return {
        "positions": [{"code": "600000", "shares": 1000, "current_price": 10.0}],
        "cash": 0.0,
    }


def test_var_and_cvar_pick_the_empirical_tail():
    """95% 置信、100 个观测 → tail_size=5：VaR 取第 5 差，CVaR 取最差 5 天均值。"""
    returns = [-0.10, -0.08, -0.06, -0.04, -0.02] + [0.01] * 95

    var_pct, cvar_pct = _historical_var_cvar(returns)

    assert var_pct == pytest.approx(2.0)
    assert cvar_pct == pytest.approx(6.0)
    assert cvar_pct > var_pct


def test_tail_metrics_require_a_minimum_sample():
    """样本不足返回 None，绝不返回 0.0——空/薄样本不得被读成零风险。"""
    assert _historical_var_cvar([-0.01] * (MIN_TAIL_OBSERVATIONS - 1)) is None
    assert _historical_var_cvar([]) is None
    assert _historical_var_cvar([-0.01] * MIN_TAIL_OBSERVATIONS) is not None


def test_build_tail_risk_evidence_reports_var_beta_and_scenarios():
    holding = [0.01, -0.02] * 45
    benchmark = [0.005, -0.01] * 45
    evidence = build_tail_risk_evidence(
        _portfolio(),
        bars_by_code={"600000": _bars(holding)},
        benchmark_bars=_bars(benchmark),
        decision_asof=ASOF,
    )

    assert evidence["schema"] == "portfolio_tail_risk_v1"
    assert evidence["observations"] >= MIN_TAIL_OBSERVATIONS
    assert evidence["missing_reasons"] == []
    assert evidence["var_pct"] > 0
    assert evidence["cvar_pct"] >= evidence["var_pct"]
    assert evidence["worst_observed_day_pct"] < 0
    # 单一持仓、收益恰为基准的两倍 → beta ≈ 2
    assert evidence["portfolio_beta"] == pytest.approx(2.0, abs=1e-6)
    assert evidence["advisory_only"] is True

    scenarios = evidence["stress_scenarios"]
    assert len(scenarios) == len(STRESS_BENCHMARK_SHOCKS_PCT)
    assert scenarios[0]["benchmark_shock_pct"] == STRESS_BENCHMARK_SHOCKS_PCT[0]
    # beta≈2 下 -3% 基准冲击 → 组合 ≈ -6%
    assert scenarios[0]["projected_portfolio_pct"] == pytest.approx(-6.0, abs=1e-3)
    assert all(item["projected_portfolio_pct"] < 0 for item in scenarios)


def test_thin_history_yields_none_and_a_named_reason():
    holding = [0.01, -0.02] * 5
    evidence = build_tail_risk_evidence(
        _portfolio(),
        bars_by_code={"600000": _bars(holding)},
        benchmark_bars=_bars([0.005, -0.01] * 5),
        decision_asof=ASOF,
    )

    assert evidence["var_pct"] is None
    assert evidence["cvar_pct"] is None
    assert evidence["stress_scenarios"] == []
    assert "holding_history_missing:600000" in evidence["missing_reasons"]


def test_empty_portfolio_is_not_reported_as_zero_risk():
    evidence = build_tail_risk_evidence(
        {"positions": [], "cash": 100_000.0},
        bars_by_code={},
        benchmark_bars=_bars([0.005, -0.01] * 45),
        decision_asof=ASOF,
    )

    assert evidence["observations"] == 0
    assert evidence["var_pct"] is None
    assert evidence["portfolio_beta"] is None
    assert "portfolio_history_missing" in evidence["missing_reasons"]
    assert "tail_sample_insufficient" in evidence["missing_reasons"]
