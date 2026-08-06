"""Cron budget reports combine observed latency with dependency worst cases."""

from __future__ import annotations

import json
import os
import tempfile

from scripts.cron_budget_report import (
    MARGIN_FACTOR,
    MIN_MARGIN_SAMPLES,
    build_budget_report,
    build_push_report,
    main,
)


def _manifest(timeout_seconds: int) -> dict:
    return {
        "jobs": [{
            "id": "demo",
            "enabled": True,
            "context_from": [],
            "run": {"timeout_seconds": timeout_seconds},
        }]
    }


def _runs(duration: float, count: int, status: str = "ok") -> list[dict]:
    return [
        {"job_id": "demo", "duration_seconds": duration, "status": status}
        for _ in range(count)
    ]


def test_budget_report_computes_deterministic_p95_and_p99():
    manifest = {
        "jobs": [{
            "id": "demo",
            "enabled": True,
            "context_from": [],
            "run": {"timeout_seconds": 30},
        }]
    }
    runs = [
        {"job_id": "demo", "duration_seconds": value, "status": "ok"}
        for value in range(1, 21)
    ]

    report = build_budget_report(manifest, runs)
    demo = report["jobs"]["demo"]

    assert demo["samples"] == 20
    assert demo["observed_p95_seconds"] == 19.0
    assert demo["observed_p99_seconds"] == 20.0
    assert demo["dependency_worst_case_seconds"] == 120


def test_push_report_aggregates_delivery_and_character_metrics():
    records = [
        {
            "job_id": "alpha",
            "trading_date": "2026-06-10",
            "delivered": True,
            "output_chars": 100,
            "was_compressed": False,
            "silent_reason": "none",
        },
        {
            "job_id": "alpha",
            "trading_date": "2026-06-10",
            "delivered": False,
            "output_chars": 0,
            "was_compressed": False,
            "silent_reason": "no_signal",
        },
        {
            "job_id": "alpha",
            "trading_date": "2026-06-11",
            "delivered": True,
            "output_chars": 50,
            "was_compressed": True,
            "silent_reason": "none",
        },
        {
            "job_id": "beta",
            "trading_date": "2026-06-10",
            "delivered": True,
            "output_chars": 200,
            "was_compressed": False,
            "silent_reason": "none",
        },
    ]

    report = build_push_report(records)

    assert report["schema"] == "a_stock_push_telemetry_report_v1"
    assert report["jobs"]["alpha"] == {
        "sample_days": 2,
        "runs": 3,
        "daily_avg_pushes": 1.0,
        "daily_avg_chars": 75.0,
        "silent_rate": 0.333,
        "compression_rate": 0.333,
    }
    assert report["jobs"]["beta"]["daily_avg_chars"] == 200.0
    assert report["char_top5"] == [
        {"job_id": "beta", "output_chars": 200},
        {"job_id": "alpha", "output_chars": 150},
    ]
    assert report["daily_total_push_chars"] == {
        "2026-06-10": 300,
        "2026-06-11": 50,
    }


def test_thin_timeout_margin_is_reported_as_a_warning():
    """A job whose p95 sits close under its timeout is one slow tail from dying.

    news-monitor in production (2026-08): p95 126.4s under a 180s timeout.
    """
    report = build_budget_report(_manifest(180), _runs(126.4, MIN_MARGIN_SAMPLES))
    demo = report["jobs"]["demo"]

    assert demo["margin_status"] == "thin_margin"
    assert demo["margin_ratio"] == 1.424
    assert demo["recommended_timeout_seconds"] == 380  # ceil(126.4 * 3)
    assert [item["job_id"] for item in report["warnings"]] == ["demo"]
    assert report["advisories"] == []


def test_recommendation_never_falls_below_the_manifest_timeout():
    """Only-raise semantics.

    The sample set is survivor-biased: ``build_budget_report`` reads ok runs
    only, and before the P0 fix (PR #162) timed-out runs left no artifact at
    all — so history contains the fast successes and none of the slow deaths.
    candidate-preopen shows p95 0.151s purely because every recorded run hit
    the ``bootstrap_status=reused_existing`` fast path, while its cold path is
    the 600s one that actually timed out. A derived budget may therefore raise
    a timeout, never lower one.
    """
    report = build_budget_report(_manifest(600), _runs(0.151, 40))
    demo = report["jobs"]["demo"]

    assert demo["recommended_timeout_seconds"] == 600
    assert demo["margin_status"] == "ok"
    assert report["warnings"] == []
    assert report["margin_policy"]["adjustment"] == "raise_only"


def test_scarce_samples_never_produce_a_warning():
    """Below the sample floor the p95 is just max(samples); no verdict.

    Kept as an advisory so ops can still see it, but it must not be a warning
    and must not fail ``--fail-on-warn``.
    """
    report = build_budget_report(_manifest(120), _runs(105.5, MIN_MARGIN_SAMPLES - 1))
    demo = report["jobs"]["demo"]

    assert demo["margin_status"] == "insufficient_samples"
    assert report["warnings"] == []
    assert [item["job_id"] for item in report["advisories"]] == ["demo"]


def test_healthy_and_unobserved_jobs_stay_out_of_both_lists():
    healthy = build_budget_report(_manifest(120), _runs(1.0, MIN_MARGIN_SAMPLES))
    assert healthy["jobs"]["demo"]["margin_status"] == "ok"
    assert healthy["jobs"]["demo"]["margin_ratio"] == 120.0
    assert healthy["warnings"] == [] and healthy["advisories"] == []

    unobserved = build_budget_report(_manifest(120), [])
    assert unobserved["jobs"]["demo"]["margin_status"] == "no_samples"
    assert unobserved["jobs"]["demo"]["recommended_timeout_seconds"] == 120
    assert unobserved["warnings"] == [] and unobserved["advisories"] == []


def test_failed_runs_are_not_counted_as_observations():
    report = build_budget_report(_manifest(120), _runs(400.0, 20, status="timeout"))

    assert report["jobs"]["demo"]["samples"] == 0
    assert report["jobs"]["demo"]["margin_status"] == "no_samples"


def test_margin_policy_is_multiplicative_only():
    """No additive grace here.

    ``dependency_timeout_budget`` adds a flat 60s because it sums a chain. Per
    job that constant would flag every short job unconditionally: production
    paper-trading-monitor (p95 0.717s, timeout 30s) would need 62s.
    """
    report = build_budget_report(_manifest(30), _runs(0.717, 30))

    assert report["margin_policy"] == {
        "margin_factor": MARGIN_FACTOR,
        "min_samples": MIN_MARGIN_SAMPLES,
        "adjustment": "raise_only",
        "sample_bias": "ok_runs_only",
    }
    assert report["jobs"]["demo"]["margin_status"] == "ok"


def _write(payload) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
        json.dump(payload, handle)
        return handle.name


def test_fail_on_warn_exit_code_follows_warnings_only(monkeypatch, capsys):
    manifest_path = _write(_manifest(180))
    thin_path = _write(_runs(126.4, MIN_MARGIN_SAMPLES))
    scarce_path = _write(_runs(105.5, MIN_MARGIN_SAMPLES - 1))
    try:
        argv = ["cron_budget_report.py", "--manifest", manifest_path,
                "--runs", thin_path, "--fail-on-warn"]
        monkeypatch.setattr("sys.argv", argv)
        assert main() == 1
        capsys.readouterr()

        monkeypatch.setattr("sys.argv", argv[:-1])
        assert main() == 0
        capsys.readouterr()

        monkeypatch.setattr("sys.argv", ["cron_budget_report.py", "--manifest",
                                         manifest_path, "--runs", scarce_path,
                                         "--fail-on-warn"])
        assert main() == 0
    finally:
        for path in (manifest_path, thin_path, scarce_path):
            os.unlink(path)


def test_push_report_handles_empty_data():
    report = build_push_report([])

    assert report["status"] == "ok"
    assert report["jobs"] == {}
    assert report["char_top5"] == []
    assert report["daily_total_push_chars"] == {}
