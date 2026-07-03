"""Serenity coverage report tests — top-N fresh deep-research coverage (§6c)."""

import os
import sys

import pytest

BASE = os.path.dirname(__file__)
PROJ = os.path.abspath(os.path.join(BASE, ".."))
sys.path.insert(0, os.path.join(PROJ, "skills", "common"))
sys.path.insert(0, PROJ)
# Append (not prepend): scripts/ holds exec-at-import compatibility wrappers
# that must never shadow the canonical modules under skills/*/scripts for
# other test files in the same session.
sys.path.append(os.path.join(PROJ, "scripts"))

import serenity_coverage_report as report  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import atomic_write_json  # noqa: E402


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    return tmp_path


def _write_pool(codes):
    atomic_write_json(
        data_file("stock-triage", "candidate_pool_latest.json"),
        {
            "status": "ready",
            "trading_date": "2026-07-02",
            "candidates": [
                {"code": code, "name": f"股票{code}"} for code in codes
            ],
        },
    )


def test_no_pool_reports_no_candidate_pool():
    result = report.build_report()
    assert result["status"] == "no_candidate_pool"
    assert result["coverage_pct"] is None


def test_not_ready_pool_is_treated_as_no_pool():
    atomic_write_json(
        data_file("stock-triage", "candidate_pool_latest.json"),
        {"status": "building", "candidates": [{"code": "600519"}]},
    )
    result = report.build_report()
    assert result["status"] == "no_candidate_pool"


def test_coverage_counts_only_fresh_non_stale_entries():
    codes = [f"{i:06d}" for i in range(1, 6)]
    _write_pool(codes)

    def lookup(code):
        if code == "000001":
            return {"found": True, "code": code, "asof": "2026-07-01", "stale": False, "deep_score": 8.0}
        if code == "000002":
            return {"found": True, "code": code, "asof": "2026-01-01", "stale": True, "deep_score": 5.0}
        return None

    result = report.build_report(top_n=10, cache_lookup=lookup)

    assert result["status"] == "ok"
    assert result["total"] == 5
    assert result["fresh"] == 1
    assert result["coverage_pct"] == 20.0
    by_code = {row["code"]: row for row in result["candidates"]}
    assert by_code["000001"]["fresh"] is True
    assert by_code["000002"]["fresh"] is False
    assert by_code["000002"]["stale"] is True
    assert by_code["000003"]["found"] is False


def test_top_n_limits_the_candidate_slice():
    codes = [f"{i:06d}" for i in range(1, 15)]
    _write_pool(codes)

    result = report.build_report(top_n=10, cache_lookup=lambda code: None)

    assert result["total"] == 10


def test_full_coverage_is_100_percent():
    codes = ["600519", "000001"]
    _write_pool(codes)

    result = report.build_report(
        top_n=10,
        cache_lookup=lambda code: {
            "found": True, "code": code, "asof": "2026-07-02",
            "stale": False, "deep_score": 9.0,
        },
    )

    assert result["fresh"] == 2
    assert result["coverage_pct"] == 100.0
