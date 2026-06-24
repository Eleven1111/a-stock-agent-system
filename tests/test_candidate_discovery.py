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


def test_preopen_bootstrap_reuses_only_ready_recent_nonfuture_pool():
    ready = {
        "status": "ready",
        "asof": "2026-06-19",
        "eligible_count": 1,
        "auction_scan_codes": ["sh600001"],
        "candidates": [{"code": "600001"}],
    }

    assert discovery.reusable_pool(ready, "2026-06-22") is True
    assert discovery.reusable_pool({**ready, "asof": "2026-06-17"}, "2026-06-22") is False
    assert discovery.reusable_pool({**ready, "asof": "2026-06-23"}, "2026-06-22") is False
    assert discovery.reusable_pool({**ready, "status": "insufficient_data"}, "2026-06-22") is False
    assert discovery.reusable_pool({**ready, "candidates": []}, "2026-06-22") is False
    legacy = {key: value for key, value in ready.items() if key != "auction_scan_codes"}
    assert discovery.reusable_pool(legacy, "2026-06-22") is False


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
    assert latest["auction_scan_count"] == 12
    assert len(latest["auction_scan_codes"]) == 12
    assert lifecycle["metadata"]["scanned_count"] == 12
    assert len(lifecycle["records"]) == 12
    assert sum(record["current_stage"] == "watch_pool" for record in lifecycle["records"]) == 6
    selected_lifecycle = next(
        record for record in lifecycle["records"]
        if record["current_stage"] == "watch_pool"
    )
    assert selected_lifecycle["strategy_id"] == "trend_pullback"
    assert selected_lifecycle["selection_context"]["window"] == "D0_close"
    assert latest["input_snapshot"]["snapshot_id"].startswith("snap-")
    assert latest["input_snapshot"]["consumed_from_snapshot"] is True
    selection = read_json(discovery.hot_money_selection_file(), {})
    assert selection["schema"] == "hot_money_selection_state_v1"
    assert selection["snapshot"]["snapshot_id"].startswith("snap-")
    assert selection["status"] == "insufficient_data"
    assert all(
        not item["selected_by"]["daban"]
        for item in latest["candidates"]
    )
    assert all("selection_context" in item for item in latest["candidates"])
    report = discovery.json_report(result)
    assert "rejected" not in report
    assert len(report["top_candidates"]) == 5
    assert report["hot_money_selection"]["status"] == "insufficient_data"
    assert "sector_rank" in report["top_candidates"][0]
    assert "leader_rank" in report["top_candidates"][0]


def test_run_discovery_injects_cached_industry_into_sector(tmp_path, monkeypatch):
    """注入的行业映射应填补沪市空缺并经由 quotes 进入候选的 sector。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    listed_date = (date.today() - timedelta(days=500)).isoformat()
    # 沪市主板代码：上市列表本不带行业，全靠映射补
    universe = [
        {"code": f"6005{i:02d}", "name": f"股票{i}", "listed_date": listed_date}
        for i in range(12)
    ]
    quote_fields = {
        item["code"]: {
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
    sector_map = {item["code"]: "半导体" for item in universe}

    # 如实复现 fetch_universe_quotes：把(已富化的)universe 记录并入 quote
    def quote_fetcher(enriched_universe):
        return {
            item["code"]: {**item, **quote_fields[item["code"]], "code": item["code"]}
            for item in enriched_universe
        }

    result = discovery.run_discovery(
        "2026-06-10",
        watch_limit=6,
        prefilter_limit=10,
        universe_fetcher=lambda: universe,
        quote_fetcher=quote_fetcher,
        kline_fetcher=lambda candidates: {
            item["code"]: klines[item["code"]] for item in candidates
        },
        industry_provider=lambda _asof: sector_map,
        settle_previous=False,
    )

    assert result["candidate_count"] == 6
    assert all(item.get("sector") == "半导体" for item in result["candidates"])
    # 板块归属齐备 → 选股态可识别该主线
    selection = read_json(discovery.hot_money_selection_file(), {})
    assert any(row.get("sector") == "半导体" for row in selection.get("sectors") or [])


def test_run_discovery_industry_injection_is_noop_without_cache(tmp_path, monkeypatch):
    """缓存缺失时默认 provider 返回空映射，主链零回归。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert discovery.load_cached_industry("2026-06-10") == {}


def test_run_discovery_reconciles_daily_observation_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    listed_date = (date.today() - timedelta(days=500)).isoformat()
    universe = [
        {"code": f"600{i:03d}", "name": f"股票{i}", "listed_date": listed_date}
        for i in range(10)
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
    captured = {}
    monkeypatch.setattr(
        discovery.monitor_registry,
        "reconcile_automatic",
        lambda kind, targets, **kwargs: captured.update({
            "kind": kind,
            "targets": list(targets),
            **kwargs,
        }) or {"activated": [], "deactivated": [], "skipped": {}},
    )

    result = discovery.run_discovery(
        "2026-06-10",
        watch_limit=5,
        prefilter_limit=10,
        universe_fetcher=lambda: universe,
        quote_fetcher=lambda _universe: quote_map,
        kline_fetcher=lambda candidates: {
            item["code"]: klines[item["code"]] for item in candidates
        },
        settle_previous=False,
    )

    assert captured["kind"] == "stock"
    assert captured["source"] == "candidate_discovery"
    assert captured["source_group"] == "daily_observation"
    assert captured["trading_date"] == "2026-06-10"
    assert captured["batch_id"] == "a-share-20260610"
    assert {"daily_observation", "event_watch", "auction_shortlist", "open_confirmation"}.issubset(
        set(captured["replace_source_groups"])
    )
    assert [item["code"] for item in captured["targets"]] == [
        item["code"] for item in result["candidates"]
    ]


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


def test_discovery_keeps_same_day_social_attention_when_ladder_is_stale(monkeypatch):
    import signal_context

    monkeypatch.setattr(
        signal_context,
        "read_signal_context",
        lambda: {
            "ladder_asof": "2026-06-01",
            "lianban_ladder": {"600001": {"lianban": 8}},
            "social_attention_asof": "2026-06-11",
            "social_attention": {
                "schema": "social_attention_snapshot_v1",
                "trading_date": "2026-06-11",
                "stocks": {"600001": {"eligible_for_boost": True}},
            },
        },
    )

    signal_ctx, temperature = discovery.load_signal_context_for_discovery("2026-06-11")

    assert temperature["context_fresh"] is False
    assert "lianban_ladder" not in signal_ctx
    assert signal_ctx["social_attention"]["trading_date"] == "2026-06-11"


def test_discovery_drops_stale_social_attention_from_fresh_ladder(monkeypatch):
    import signal_context

    monkeypatch.setattr(
        signal_context,
        "read_signal_context",
        lambda: {
            "ladder_asof": "2026-06-11",
            "lianban_ladder": {"600001": {"lianban": 2}},
            "social_attention_asof": "2026-06-10",
            "social_attention": {
                "schema": "social_attention_snapshot_v1",
                "trading_date": "2026-06-10",
                "stocks": {"600001": {"eligible_for_boost": True}},
            },
        },
    )

    signal_ctx, temperature = discovery.load_signal_context_for_discovery("2026-06-11")

    assert temperature["context_fresh"] is True
    assert "lianban_ladder" in signal_ctx
    assert "social_attention" not in signal_ctx
