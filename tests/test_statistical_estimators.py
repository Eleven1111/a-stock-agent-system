"""Numeric contracts for the deflated Sharpe / CSCV estimators.

The expected values here are produced by reference implementations written
directly from Bailey & Lopez de Prado (2014) inside this module, using
``math.erf`` for the normal CDF rather than the ``statistics.NormalDist`` path
the production code takes.  Nothing in this file asks the function under test
for its own expectation.
"""

from __future__ import annotations

import math
import random
import statistics
from statistics import NormalDist

import pytest

from validation_program import (
    STATISTICS_COMPUTED_BY,
    STATISTICS_SCHEMA_VERSION,
    compute_effective_samples,
    compute_statistical_validation,
    deflated_sharpe,
    observed_trial_sharpes,
    probability_of_backtest_overfitting,
    verify_validation_artifact,
)

EULER_MASCHERONI = 0.5772156649015329


def _normal_cdf(value: float) -> float:
    """Independent normal CDF: error function rather than NormalDist."""

    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _reference_deflated_sharpe(series, trial_sharpes):
    """Bailey & Lopez de Prado (2014), transcribed from the paper."""

    count = len(series)
    mean = statistics.fmean(series)
    std = statistics.stdev(series)
    sharpe = mean / std
    standardised = [(value - mean) / std for value in series]
    skew = statistics.fmean(value**3 for value in standardised)
    kurtosis = statistics.fmean(value**4 for value in standardised)
    trials = len(trial_sharpes)
    dispersion = math.sqrt(statistics.variance(trial_sharpes))
    quantile = NormalDist()
    threshold = dispersion * (
        (1 - EULER_MASCHERONI) * quantile.inv_cdf(1 - 1 / trials)
        + EULER_MASCHERONI * quantile.inv_cdf(1 - 1 / (trials * math.e))
    )
    numerator = (sharpe - threshold) * math.sqrt(count - 1)
    denominator = math.sqrt(1 - skew * sharpe + ((kurtosis - 1) / 4) * sharpe * sharpe)
    return threshold, _normal_cdf(numerator / denominator)


def _legacy_expected_maximum(trials: int) -> float:
    """The superseded threshold: a bare standard-normal quantile, no dispersion."""

    return NormalDist().inv_cdf(1 - 1 / (trials + 1))


def _variant_series(seed: int, drift: float, count: int = 400) -> list[float]:
    rng = random.Random(seed)
    return [drift + rng.gauss(0, 0.01) for _ in range(count)]


def test_deflated_sharpe_matches_paper_reference_and_rejects_the_legacy_threshold():
    variants = {f"v{index}": _variant_series(100 + index, 0.0004 * index) for index in range(6)}
    trials = observed_trial_sharpes(variants)
    trial_sharpes = list(trials["sharpes"].values())
    primary = variants["v5"]

    result = deflated_sharpe(primary, trials=6, trial_sharpes=trial_sharpes)
    threshold, probability = _reference_deflated_sharpe(primary, trial_sharpes)

    assert result["status"] == "evaluated"
    assert result["expected_maximum_sharpe"] == pytest.approx(threshold, rel=1e-12)
    assert result["probability"] == pytest.approx(probability, rel=1e-9)
    assert result["effective_trials"] == 6
    assert result["trial_sharpe_variance"] == pytest.approx(statistics.variance(trial_sharpes))
    assert result["threshold_source"] == "observed_trial_sharpe_variance"
    assert result["method_version"] == "deflated_sharpe-v2"

    # The defect being fixed: the old threshold ignored cross-trial dispersion and
    # therefore lived on a completely different scale from a per-period Sharpe.
    assert _legacy_expected_maximum(6) > 5 * result["expected_maximum_sharpe"]
    assert result["expected_maximum_sharpe"] < max(trial_sharpes) * 2


def test_deflated_sharpe_threshold_scales_with_trial_dispersion():
    primary = _variant_series(7, 0.0006)
    base = [0.05, 0.10, 0.15, 0.30]
    doubled = [value * 2 for value in base]

    narrow = deflated_sharpe(primary, trials=4, trial_sharpes=base)
    wide = deflated_sharpe(primary, trials=4, trial_sharpes=doubled)

    assert wide["expected_maximum_sharpe"] == pytest.approx(
        2 * narrow["expected_maximum_sharpe"], rel=1e-12
    )
    assert wide["probability"] < narrow["probability"]


def test_deflated_sharpe_refuses_to_invent_a_trial_dispersion():
    rng = random.Random(11)
    series = [0.001 + rng.gauss(0, 0.01) for _ in range(10000)]

    missing = deflated_sharpe(series, trials=6)
    assert missing == {
        "status": "not_evaluated",
        "reason": "trial_dispersion_unavailable",
        "input_sha256": missing["input_sha256"],
        "config_sha256": missing["config_sha256"],
    }

    degenerate = deflated_sharpe(series, trials=3, trial_sharpes=[0.2, 0.2, 0.2])
    assert degenerate["reason"] == "trial_dispersion_degenerate"

    single = deflated_sharpe(series, trials=1)
    assert single["method"] == "probabilistic_sharpe_ratio"
    assert single["expected_maximum_sharpe"] == 0.0
    assert single["threshold_source"] == "single_trial_no_deflation"

    assert deflated_sharpe([0.01] * 5, trials=1)["status"] == "not_evaluated"
    assert deflated_sharpe([0.01] * 40, trials=1)["reason"] == "degenerate_sample"


def test_observed_trial_sharpes_reports_what_it_could_not_use():
    trials = observed_trial_sharpes(
        {
            "good": _variant_series(3, 0.001, count=40),
            "flat": [0.01] * 40,
            "short": [0.01, 0.02],
        },
        minimum_observations=20,
    )
    assert set(trials["sharpes"]) == {"good"}
    assert trials["excluded"] == {"flat": "degenerate_sample", "short": "sample_insufficient"}
    assert trials["frequency_consistent"] is True
    assert trials["observation_count"] == 40


def test_pbo_collapses_indistinguishable_variants_instead_of_scoring_them():
    series = _variant_series(21, 0.0005, count=40)
    result = probability_of_backtest_overfitting({"a": series, "b": list(series)}, partitions=4)

    assert result["status"] == "not_evaluated"
    assert result["reason"] == "insufficient_distinct_variants"
    assert result["duplicate_groups"] == [["a", "b"]]
    assert result["distinct_variants"] == ["a"]
    assert "pbo" not in result


def test_pbo_is_invariant_to_variant_names_and_insertion_order():
    variants = {
        "alpha": _variant_series(31, 0.0008, count=40),
        "beta": _variant_series(32, 0.0002, count=40),
        "gamma": _variant_series(33, 0.0005, count=40),
    }
    renamed = {
        "zzz": variants["alpha"],
        "aaa": variants["beta"],
        "mmm": variants["gamma"],
    }
    reordered = {key: variants[key] for key in ("gamma", "beta", "alpha")}

    baseline = probability_of_backtest_overfitting(variants, partitions=4)
    assert baseline["status"] == "evaluated"
    assert probability_of_backtest_overfitting(renamed, partitions=4)["pbo"] == baseline["pbo"]
    assert probability_of_backtest_overfitting(reordered, partitions=4)["pbo"] == baseline["pbo"]


def test_pbo_ties_are_shared_rather_than_broken_by_name():
    # Two variants tie exactly in-sample and out-of-sample; the third is strictly
    # worse.  Lexical tie-breaking would pin the outcome on one arbitrary name.
    left = [0.01, -0.01, 0.02, -0.02, 0.03, -0.03, 0.04, -0.04]
    right = [0.02, -0.02, 0.01, -0.01, 0.04, -0.04, 0.03, -0.03]
    poor = [-0.05] * 8
    result = probability_of_backtest_overfitting(
        {"a": left, "b": right, "c": poor}, partitions=4
    )
    swapped = probability_of_backtest_overfitting(
        {"z": left, "y": right, "c": poor}, partitions=4
    )
    assert result["status"] == "evaluated"
    assert result["pbo"] == swapped["pbo"]


def test_pbo_discloses_metrics_resolution_exclusions_and_embargo_state():
    variants = {
        "alpha": _variant_series(41, 0.0008, count=40),
        "beta": _variant_series(42, 0.0002, count=40),
        "short": [0.01, 0.02, 0.03],
        "ragged": _variant_series(43, 0.0004, count=36),
    }
    result = probability_of_backtest_overfitting(variants, partitions=4)

    assert result["selection_metric"] == "in_sample_mean_return"
    assert result["ranking_metric"] == "out_of_sample_mean_return"
    assert result["combinations"] == 6
    assert result["resolution"] == pytest.approx(1 / 6)
    assert result["excluded_variants"] == {"ragged": "length_mismatch", "short": "series_invalid"}
    assert result["purge_embargo"] == {"observations": 0, "applied": False}

    embargoed = probability_of_backtest_overfitting(variants, partitions=4, embargo=2)
    assert embargoed["purge_embargo"] == {"observations": 2, "applied": True}
    assert probability_of_backtest_overfitting(
        variants, partitions=4, embargo=10
    )["reason"] == "embargo_exhausted_test_fold"


def test_pbo_separates_an_overfit_selection_from_a_stable_one():
    # Each variant is tuned to exactly one quarter of the sample, so whichever
    # one wins in-sample is by construction among the worst out-of-sample.
    overfit = {
        f"tuned_{group}": [
            0.1 if index // 4 == group else -0.01 for index in range(16)
        ]
        for group in range(4)
    }
    stable = {
        "best": [0.01] * 16,
        "middle": [0.005] * 16,
        "worst": [0.001] * 16,
    }
    assert probability_of_backtest_overfitting(overfit, partitions=4)["pbo"] > 0.5
    assert probability_of_backtest_overfitting(stable, partitions=4)["pbo"] == 0.0


def test_effective_samples_separate_breadth_from_independence():
    same_day = [
        {"trade_id": str(index), "stock": f"{index:06}", "session": "2026-09-02", "regime": "S3"}
        for index in range(30)
    ]
    samples = compute_effective_samples(same_day)

    assert samples["trade"] == 30.0
    assert samples["session"] == 1.0
    assert samples["distinct_sessions"] == 1
    assert samples["basis"] == "kish_breadth"
    assert samples["autocorrelation_adjusted"] is False
    assert samples["sector"] is None
    assert samples["sector_status"] == "unavailable"

    with_sector = compute_effective_samples(
        [{**trade, "sector": "银行" if index % 2 else "军工"} for index, trade in enumerate(same_day)]
    )
    assert with_sector["sector_status"] == "evaluated"
    assert with_sector["sector"] == pytest.approx(2.0)


def test_statistical_suite_binds_its_trial_set_and_supersedes_v1_artifacts():
    returns = _variant_series(51, 0.0009, count=60)
    suite = compute_statistical_validation(
        primary_variant="a",
        variant_returns={
            "a": returns,
            "b": [-value for value in returns],
            "flat": [0.01] * 60,
        },
        p_values={"a": 0.01, "b": 0.2, "flat": 0.5},
        config={
            "minimum_observations": 20, "block_length": 4, "bootstrap_resamples": 200,
            "fdr_alpha": 0.05, "hac_lags": 3, "pbo_partitions": 4,
            "maximum_pbo": 0.5, "minimum_deflated_sharpe_probability": 0.95,
        },
        seed=7,
    )

    assert suite["schema_version"] == STATISTICS_SCHEMA_VERSION == "statistical-validation-suite-v2"
    assert suite["computed_by"] == STATISTICS_COMPUTED_BY
    assert suite["trial_set"]["included"] == ["a", "b"]
    assert suite["trial_set"]["excluded"] == {"flat": "degenerate_sample"}
    assert suite["trial_set"]["observation_count"] == 60
    assert suite["calculations"]["deflated_sharpe"]["threshold_source"] == (
        "observed_trial_sharpe_variance"
    )
    assert verify_validation_artifact(suite) is True

    superseded = {**suite, "schema_version": "statistical-validation-suite-v1"}
    superseded["artifact_sha256"] = suite["artifact_sha256"]
    assert verify_validation_artifact(superseded) is False
