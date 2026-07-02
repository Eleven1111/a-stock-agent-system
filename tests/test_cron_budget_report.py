"""Cron budget reports combine observed latency with dependency worst cases."""

from __future__ import annotations

from scripts.cron_budget_report import build_budget_report, build_push_report


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


def test_push_report_handles_empty_data():
    report = build_push_report([])

    assert report["status"] == "ok"
    assert report["jobs"] == {}
    assert report["char_top5"] == []
    assert report["daily_total_push_chars"] == {}
