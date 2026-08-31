from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

import pytest

import skills.common  # noqa: F401
import exploratory_strategy_baseline as baseline


ROOT = Path(__file__).resolve().parents[1]


def _policy() -> dict:
    value = baseline.load_policy(ROOT / "config" / "exploratory_strategy_baseline.json")
    value["minimum_history_bars"] = 20
    value["walk_forward"] = {
        "train_size": 24, "calibration_size": 6, "test_size": 6,
        "step": 6, "purge": 3, "embargo": 1, "mode": "expanding",
        "minimum_selection_samples": 1,
    }
    for strategy in value["strategies"].values():
        strategy["variants"] = [strategy["variants"][0]]
    return value


def _rows(days: int = 70) -> list[dict]:
    rows = []
    for code, scale, drift in (("000300", 100.0, 0.001), ("600001", 10.0, 0.002), ("000001", 12.0, 0.0005)):
        price = scale
        for day in range(days):
            date = f"2026-{1 + day // 28:02d}-{1 + day % 28:02d}"
            previous = price
            price *= 1.0 + drift + (0.004 if day % 11 == 0 else -0.001 if day % 7 == 0 else 0.0)
            open_ = previous * (1.006 if day % 9 == 0 else 1.0)
            rows.append({
                "code": code, "trading_date": date, "open": open_,
                "high": max(open_, price) * 1.01, "low": min(open_, price) * 0.99,
                "close": price, "volume": 1000.0 * (1.6 if day % 9 == 0 else 1.0),
            })
    return rows


def test_policy_is_fail_closed_and_excludes_s2_s4():
    policy = baseline.load_policy(ROOT / "config" / "exploratory_strategy_baseline.json")
    assert policy["qualification"] == {
        "evidence_class": "exploratory_reconstruction",
        "research_gate_eligible": False,
        "registry_eligible": False,
        "live_weight_eligible": False,
    }
    sources = {row["source_strategy"] for row in policy["strategies"].values()}
    assert "divergence_reseal" not in sources
    assert "preleader_arbitrage" not in sources


def test_run_reports_walk_forward_regime_metrics_and_no_live_eligibility():
    report = baseline.run(_rows(), _policy())
    assert report["schema"] == "exploratory_strategy_baseline_v1"
    assert report["qualification"]["research_gate_eligible"] is False
    assert report["point_in_time"]["entry_rule"] == "next_trading_session_open"
    assert report["point_in_time"]["purge_sessions"] == 3
    assert set(report["strategies"]) == set(baseline.ALLOWED_STRATEGIES)
    assert report["coverage"]["decision_windows_considered"] > 0
    assert report["coverage"]["decision_windows_available"] > 0
    assert 0 < report["coverage"]["decision_window_coverage_ratio"] <= 1
    for result in report["strategies"].values():
        assert set(result["by_regime"]) == {"up", "range", "down", "unavailable"}
        assert "parameter_stability" in result
        assert set(result["overall"]) >= {"sample_count", "mean_net_return", "sum_net_return", "mean_excess_return", "hit_rate"}


def test_future_bars_do_not_change_earlier_fold_results():
    policy = _policy()
    first = baseline.run(_rows(70), policy)
    second = baseline.run(_rows(76), policy)
    for strategy_id in baseline.ALLOWED_STRATEGIES:
        first_folds = first["strategies"][strategy_id]["folds"]
        second_folds = second["strategies"][strategy_id]["folds"]
        assert second_folds[:len(first_folds)] == first_folds


def test_missing_benchmark_fails_closed():
    with pytest.raises(ValueError, match="CSI300"):
        baseline.run([row for row in _rows() if row["code"] != "000300"], _policy())


def test_policy_rejects_live_eligibility(tmp_path):
    policy = json.loads((ROOT / "config" / "exploratory_strategy_baseline.json").read_text())
    policy["qualification"]["research_gate_eligible"] = True
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="fail closed"):
        baseline.load_policy(path)


def test_policy_rejects_purge_shorter_than_t3(tmp_path):
    policy = json.loads((ROOT / "config" / "exploratory_strategy_baseline.json").read_text())
    policy["walk_forward"]["purge"] = 2
    path = tmp_path / "bad-purge.json"
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match=r"T\+3"):
        baseline.load_policy(path)


def test_outcome_enters_next_open_and_uses_same_csi300_sessions():
    policy = _policy()
    feature = {
        "future_bars": {
            1: {"entry": {"date": "2026-02-02", "open": 10.0}, "exit": {"date": "2026-02-02", "close": 11.0}},
            3: {"entry": {"date": "2026-02-02", "open": 10.0}, "exit": {"date": "2026-02-04", "close": 12.0}},
        }
    }
    benchmark = {
        "2026-02-02": {"date": "2026-02-02", "open": 100.0, "close": 101.0},
        "2026-02-04": {"date": "2026-02-04", "open": 101.0, "close": 103.0},
    }
    expected = {"2026-02-01": {1: "2026-02-02", 3: "2026-02-04"}}
    feature["decision_date"] = "2026-02-01"
    result = baseline._outcomes(feature, benchmark, policy, expected)
    assert result[1]["benchmark_return"] == pytest.approx(0.01)
    assert result[3]["benchmark_return"] == pytest.approx(0.03)
    assert result[1]["net_return"] < 0.10  # fees and two-sided slippage are charged


def test_suspension_does_not_shift_entry_to_a_later_available_bar():
    policy = _policy()
    feature = {
        "decision_date": "2026-02-01",
        "future_bars": {
            1: {"entry": {"date": "2026-02-03", "open": 10.0}, "exit": {"date": "2026-02-03", "close": 11.0}},
            3: {"entry": {"date": "2026-02-03", "open": 10.0}, "exit": {"date": "2026-02-05", "close": 12.0}},
        },
    }
    benchmark = {
        "2026-02-02": {"open": 100.0, "close": 101.0},
        "2026-02-03": {"open": 101.0, "close": 102.0},
        "2026-02-04": {"open": 102.0, "close": 103.0},
    }
    expected = {"2026-02-01": {1: "2026-02-02", 3: "2026-02-04"}}
    assert baseline._outcomes(feature, benchmark, policy, expected) == {}


def test_sparse_production_volume_fails_closed_and_is_disclosed_without_crashing():
    rows = _rows(76)
    target = [row for row in rows if row["code"] == "600001"]
    # A stale provider hole outside a later 20-session volume window must not
    # cause float(None); windows that still depend on it are skipped honestly.
    target[10]["volume"] = None
    # A missing current OHLCV field is a decision-window data gap, never zero.
    target[42]["open"] = None
    report = baseline.run(rows, _policy())
    coverage = report["coverage"]
    reasons = coverage["decision_window_unavailable_reason_counts"]
    assert reasons["history_volume_missing_or_non_positive"] > 0
    assert reasons["decision_open_missing_or_non_positive"] == 1
    assert coverage["decision_windows_available"] < coverage["decision_windows_considered"]
    assert coverage["codes_with_features"] > 0


def test_sparse_ohlc_and_benchmark_outcomes_are_counted_not_fabricated():
    rows = _rows(76)
    stock = [row for row in rows if row["code"] == "000001"]
    stock[35]["high"] = None
    benchmark_rows = [row for row in rows if row["code"] == "000300"]
    benchmark_rows[50]["open"] = None
    benchmark_rows[54]["close"] = None
    report = baseline.run(rows, _policy())
    coverage = report["coverage"]
    assert coverage["decision_window_unavailable_reason_counts"][
        "history_high_low_close_missing_or_non_positive"
    ] > 0
    assert coverage["outcome_unavailable_reason_counts"][
        "benchmark_entry_open_missing_or_non_positive"
    ] > 0
    assert coverage["benchmark_unavailable_reason_counts"][
        "benchmark_regime_close_missing_or_non_positive"
    ] > 0


def test_old_sparse_volume_outside_required_window_does_not_poison_later_history():
    bars = [row for row in _rows(90) if row["code"] == "600001"]
    bars[0]["volume"] = None
    for row in bars:
        row["date"] = row["trading_date"]
    coverage = Counter()
    features = baseline._features_for_code(bars, 65, coverage)
    assert features
    assert coverage["windows_available"] > 0
