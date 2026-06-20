"""Cron budget reports combine observed latency with dependency worst cases."""

from __future__ import annotations

from scripts.cron_budget_report import build_budget_report


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
