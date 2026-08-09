"""Recall-loss monitoring tests; labels must never alter execution ranking."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "daban-stock-picker" / "scripts" / "auction_collector.py"
SPEC = importlib.util.spec_from_file_location("auction_collector_recall", SCRIPT)
auction = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auction)


def _quote(code: str, change: float) -> dict:
    return {
        "code": code,
        "name": "测试股",
        "price": 10.0 * (1 + change / 100),
        "prev_close": 10.0,
        "change_pct": change,
    }


def test_recall_report_computes_stage_coverage_and_loss():
    rows = [_quote("600001", 10.0), _quote("600002", 9.0), _quote("600003", 1.0)]
    report = auction.build_discovery_recall_report(
        rows,
        prefilter_codes=["600001"],
        auction_codes=["600001"],
        executable_codes=["600001"],
        open_codes=["600001"],
        asof="2026-08-08",
    )

    assert report["target_count"] == 2
    assert report["discovery_recall"] == 0.5
    assert report["auction_recall"] == 0.5
    assert report["executable_recall"] == 0.5
    assert report["staged_loss"]["outside_pool_strong_count"] == 1
    assert report["would_have_been_candidate_count"] == 1
    assert report["staged_loss"]["loss_by_stage"]["d0_prefilter_loss_count"] == 1
    assert report["staged_loss"]["d0_to_auction_lost_count"] == 0
    assert report["staged_loss"]["auction_to_open_lost_count"] == 0


def test_full_market_annotation_is_observational_only():
    quotes = {"600001": _quote("600001", 10.0), "600002": _quote("600002", 9.0)}
    annotated = auction.annotate_recall_snapshot(
        quotes,
        {
            "prefilter_codes": ["600001"],
            "full_market_codes": ["600001", "600002"],
        },
    )

    assert annotated["600001"]["outside_pool_strong"] is False
    assert annotated["600002"]["outside_pool_strong"] is True
    assert annotated["600002"]["would_have_been_candidate"] is True
    assert annotated["600002"]["snapshot_scope"] == "full_market"
