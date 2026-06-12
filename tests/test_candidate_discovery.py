"""Candidate discovery integration tests with injected data sources."""

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest
from state_store import read_json


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "stock-triage" / "scripts" / "candidate_discovery.py"
SPEC = importlib.util.spec_from_file_location("candidate_discovery", SCRIPT)
discovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discovery)


def _bars(start):
    return [
        {
            "date": f"2026-01-{i + 1:02d}",
            "open": start + i * 0.05,
            "close": start + i * 0.05 + 0.02,
            "high": start + i * 0.05 + 0.08,
            "low": start + i * 0.05 - 0.04,
            "volume": 100_000 + i * 1_000,
        }
        for i in range(60)
    ]


def test_run_discovery_persists_pool_and_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    listed_date = (date.today() - timedelta(days=500)).isoformat()
    universe = [
        {"code": f"600{i:03d}", "name": f"股票{i}", "listed_date": listed_date}
        for i in range(12)
    ]
    quote_map = {
        item["code"]: {
            **item,
            "price": 10 + i,
            "prev_close": 9.5 + i,
            "change_pct": 5 + i / 10,
            "amount": 200_000_000 + i * 10_000_000,
            "turnover": 4 + i,
            "volume": 1_000_000,
        }
        for i, item in enumerate(universe)
    }
    klines = {item["code"]: _bars(8 + i) for i, item in enumerate(universe)}

    result = discovery.run_discovery(
        "2026-06-10",
        watch_limit=6,
        prefilter_limit=10,
        universe_fetcher=lambda: universe,
        quote_fetcher=lambda _universe: quote_map,
        kline_fetcher=lambda candidates: {
            item["code"]: klines[item["code"]] for item in candidates
        },
        settle_previous=False,
    )

    assert result["status"] == "ready"
    assert result["candidate_count"] == 6
    latest = read_json(discovery.latest_pool_file(), {})
    lifecycle = read_json(discovery.candidate_lifecycle.lifecycle_file("2026-06-10"), {})
    assert latest["asof"] == "2026-06-10"
    assert len(latest["candidates"]) == 6
    assert lifecycle["metadata"]["scanned_count"] == 12
    assert len(lifecycle["records"]) == 12
    assert sum(record["current_stage"] == "watch_pool" for record in lifecycle["records"]) == 6
    assert latest["input_snapshot"]["snapshot_id"].startswith("snap-")
    assert latest["input_snapshot"]["consumed_from_snapshot"] is True
    report = discovery.json_report(result)
    assert "rejected" not in report
    assert len(report["top_candidates"]) == 5


def test_full_market_quotes_fail_closed_below_configured_coverage(monkeypatch):
    universe = [
        {"code": f"60{i:04d}", "name": f"股票{i}"}
        for i in range(1_000)
    ]

    def partial_quotes(codes):
        return {
            code: {"price": 10.0, "volume": 1_000, "amount": 100_000_000}
            for code in codes[:50]
        }

    monkeypatch.setattr(discovery, "fetch_tencent_quote", partial_quotes)

    with pytest.raises(discovery.DataSourceError, match="覆盖不足"):
        discovery.fetch_universe_quotes(universe)


def test_exchange_universe_rejects_partial_single_exchange_result(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        discovery,
        "fetch_sse_universe",
        lambda: [
            {"code": f"60{i:04d}", "exchange": "SSE"}
            for i in range(2_100)
        ],
    )
    monkeypatch.setattr(
        discovery,
        "fetch_szse_universe",
        lambda: [
            {"code": f"00{i:04d}", "exchange": "SZSE"}
            for i in range(2_200)
        ],
    )

    with pytest.raises(discovery.DataSourceError, match="SZSE=2200"):
        discovery.fetch_exchange_universe()


def test_discovery_ignores_stale_hot_money_context(monkeypatch):
    import signal_context

    monkeypatch.setattr(
        signal_context,
        "read_signal_context",
        lambda: {
            "ladder_asof": "2026-06-01",
            "lianban_ladder": {"600001": {"lianban": 8}},
            "prev_lianban_ladder": {"600001": {"lianban": 7}},
        },
    )

    signal_ctx, temperature = discovery.load_signal_context_for_discovery("2026-06-11")

    assert signal_ctx is None
    assert temperature["tier"] == "neutral"
    assert temperature["context_fresh"] is False
