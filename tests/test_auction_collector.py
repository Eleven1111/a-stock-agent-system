"""集合竞价采集器 — 五档解析 + 真竞价因子计算（纯函数，不触网）"""

import importlib.util
import json
from pathlib import Path

from a_stock_http import parse_tencent_orderbook_line, _TENCENT_BID_BASE, _TENCENT_ASK_BASE
import candidate_lifecycle
from state_store import atomic_write_json, read_json


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "daban-stock-picker" / "scripts" / "auction_collector.py"
SPEC = importlib.util.spec_from_file_location("auction_collector", SCRIPT)
ac = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ac)


def _line_with_book(bids, asks, code="sz002156"):
    """构造一行腾讯报文，把五档买卖填到 parts[9..28]。"""
    parts = [""] * 50
    for i, (p, v) in enumerate(bids):
        parts[_TENCENT_BID_BASE + 2 * i] = str(p)
        parts[_TENCENT_BID_BASE + 2 * i + 1] = str(v)
    for i, (p, v) in enumerate(asks):
        parts[_TENCENT_ASK_BASE + 2 * i] = str(p)
        parts[_TENCENT_ASK_BASE + 2 * i + 1] = str(v)
    return f'v_{code}="' + "~".join(parts) + '"'


def test_orderbook_indices_locked():
    bids = [(11.0, 50000), (10.99, 100), (10.98, 200), (10.97, 300), (10.96, 400)]
    asks = [(11.01, 600), (11.02, 700), (11.03, 800), (11.04, 900), (11.05, 1000)]
    r = parse_tencent_orderbook_line(_line_with_book(bids, asks))
    assert r is not None
    assert r["bids"][0] == (11.0, 50000.0)
    assert r["bids"][4] == (10.96, 400.0)
    assert r["asks"][0] == (11.01, 600.0)
    assert r["asks"][4] == (11.05, 1000.0)


def test_orderbook_rejects_short_line():
    assert parse_tencent_orderbook_line('v_sz002156="1~name~002156"') is None


def test_take_snapshot_uses_real_auction_provider(monkeypatch):
    monkeypatch.setattr(
        ac,
        "fetch_real_auction_snapshots",
        lambda codes, asof: (
            {"600519": [{
                "t": "09:25:00", "price": 1510.0, "matched": 3500,
                "unmatched": 0, "volume": 35.0,
                "prev_day_volume": 100000.0,
                "provider": "easy_tdx_mac_0x123d",
            }]},
            {},
        ),
    )

    assert ac.take_snapshot(["sh600519"]) == {
        "600519": [{
            "t": "09:25:00", "price": 1510.0, "matched": 3500,
            "unmatched": 0, "volume": 35.0,
            "prev_day_volume": 100000.0,
            "provider": "easy_tdx_mac_0x123d",
        }]
    }


def test_merge_auction_series_upserts_cumulative_provider_response():
    existing = [
        {"t": "09:15:00", "price": 10.0},
        {"t": "09:20:00", "price": 10.1},
    ]
    incoming = [
        {"t": "09:15:00", "price": 10.2},
        {"t": "09:20:00", "price": 10.3},
        {"t": "09:25:00", "price": 10.4},
    ]

    assert ac._merge_auction_series(existing, incoming) == [
        {"t": "09:15:00", "price": 10.2},
        {"t": "09:20:00", "price": 10.3},
        {"t": "09:25:00", "price": 10.4},
    ]


def test_merge_auction_series_preserves_stable_fields_on_lightweight_late_quote():
    existing = [{
        "t": "09:23:00",
        "price": 10.5,
        "prev_close": 10.0,
        "prev_day_volume": 1000.0,
        "prev_day_amount": 10000.0,
        "volume": 12.0,
    }]
    incoming = [{"t": "09:24:00", "price": 10.6, "volume": 13.0}]

    merged = ac._merge_auction_series(existing, incoming)

    assert merged[-1]["price"] == 10.6
    assert merged[-1]["prev_close"] == 10.0
    assert merged[-1]["prev_day_volume"] == 1000.0
    assert merged[-1]["prev_day_amount"] == 10000.0


def test_enrich_snapshot_names_fills_missing_names_without_overwriting_provider_name():
    quotes = {
        "sh600519": [{"code": "sh600519", "name": "", "price": 10.0}],
        "sh600000": {"code": "sh600000", "name": "浦发银行", "price": 9.0},
    }

    enriched = ac._enrich_snapshot_names(
        quotes,
        {"600519": "贵州茅台", "600000": "错误名称不应覆盖"},
    )

    assert enriched["sh600519"][0]["name"] == "贵州茅台"
    assert enriched["sh600000"]["name"] == "浦发银行"


def test_yiziban_detected_and_seal_ratio():
    snaps = [{
        "t": "09:24:50", "name": "通富微电", "price": 11.0, "prev_close": 10.0,
        "volume": 12000, "market_cap": 80.0,
        "matched": 1200000, "unmatched": 0,
        "bids": [(11.0, 90000)] + [(None, None)] * 4,
        "asks": [(None, None)] * 5,
    }]
    f = ac.compute_auction_factors(snaps, "sz002156", "通富微电")
    assert f["auction_gap_pct"] == 10.0
    assert f["board_status"] == "yizi_seal"
    assert f["is_yiziban"] is True
    # 封单额/流通市值 = 90000手*100*11 / (80亿) *100
    assert f["seal_amount_ratio_pct"] == round(90000 * 100 * 11.0 / (80.0e8) * 100, 3)


def test_real_matched_unmatched_fields_are_preserved_in_factors():
    f = ac.compute_auction_factors([{
        "t": "09:25:00", "name": "真实竞价", "price": 11.0, "prev_close": 10.0,
        "volume": 35.0, "matched": 3500, "unmatched": 1200,
        "prev_day_volume": 100000.0,
        "bids": [(11.0, 90000)], "asks": [(11.01, 1000)],
    }], "sz002156")

    assert f["matched"] == 3500
    assert f["unmatched"] == 1200
    assert f["auction_matched_shares"] == 3500
    assert f["auction_unmatched_shares"] == 1200


def test_high_open_bid_ask_ratio():
    snaps = [{
        "t": "09:20:00", "name": "北方稀土", "price": 21.5, "prev_close": 20.0,
        "volume": 30000, "market_cap": 300.0,
        "matched": 3000000, "unmatched": 0,
        "bids": [(21.5, 4000), (21.49, 3000), (21.48, 2000), (21.47, 1000), (21.46, 800)],
        "asks": [(21.51, 6000), (21.52, 5000), (21.53, 4000), (21.54, 3000), (21.55, 2000)],
    }]
    f = ac.compute_auction_factors(snaps, "sh600111", "北方稀土")
    assert f["board_status"] == "high_open"
    # 委买和 10800 / 委卖和 20000
    assert f["auction_bid_ask_ratio"] == round(10800 / 20000, 2)
    assert f["seal_amount_ratio_pct"] is None


def test_net_bid_delta_needs_two_post_freeze_snapshots():
    base = {"name": "X", "price": 11.0, "prev_close": 10.0, "volume": 1000,
            "market_cap": 80.0, "asks": [(None, None)] * 5}
    # 仅一个 9:20 后快照 → None
    one = [{**base, "t": "09:24:00", "bids": [(11.0, 50000)] + [(None, None)] * 4}]
    assert ac.compute_auction_factors(one, "sz002156")["auction_net_bid_delta"] is None
    # 9:18 (冻结前) 不计入，9:22→9:25 两个冻结后快照 → 委买净增
    seq = [
        {**base, "t": "09:18:00", "bids": [(11.0, 10000)] + [(None, None)] * 4},
        {**base, "t": "09:22:00", "bids": [(11.0, 40000)] + [(None, None)] * 4},
        {**base, "t": "09:24:55", "bids": [(11.0, 95000)] + [(None, None)] * 4},
    ]
    assert ac.compute_auction_factors(seq, "sz002156")["auction_net_bid_delta"] == 55000.0


def test_missing_quote_returns_error():
    assert "error" in ac.compute_auction_factors([], "sz002156")
    bad = [{"t": "09:20", "price": None, "prev_close": 10.0, "bids": [], "asks": []}]
    assert "error" in ac.compute_auction_factors(bad, "sz002156")


def test_build_result_shape():
    series = {"sz002156": [{
        "t": "09:25:00", "name": "通富微电", "price": 11.0, "prev_close": 10.0,
        "volume": 12000, "market_cap": 80.0,
        "matched": 1200000, "unmatched": 0,
        "bids": [(11.0, 90000)] + [(None, None)] * 4, "asks": [(None, None)] * 5,
    }]}
    r = ac._build_result(series, "2026-06-04")
    assert r["schema"] == "auction_factors_v1"
    assert len(r["factors"]) == 1
    assert "chanlun-backtest" in r["note"]
    assert "不是涨停概率" in r["note"]


def test_build_result_fills_missing_name_from_universe_quotes_cache(monkeypatch):
    monkeypatch.setattr(
        ac,
        "read_json",
        lambda path, default=None: {
            "quotes": {"600001": {"code": "600001", "name": "缓存名称"}}
        },
    )

    result = ac._build_result(
        {"sh600001": [{"t": "09:25:00", "price": 11.0, "prev_close": 10.0}]},
        "2026-08-24",
    )

    assert result["factors"][0]["name"] == "缓存名称"


def test_load_watch_pool_rejects_stale_state(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试
    # 状态无条件设置了它；这里要用 HERMES_HOME 驱动每个用例独立的
    # tmp_path，必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    atomic_write_json(
        ac._pool_path(),
        {"status": "ready", "asof": "2026-06-01", "candidates": [{"code": "600001"}]},
    )

    try:
        ac.load_watch_pool("2026-06-11")
    except ac.DataSourceError as exc:
        assert "过期" in str(exc)
    else:
        raise AssertionError("stale pool should fail closed")


def test_auction_scan_codes_prefer_full_eligible_universe():
    pool = {
        "candidates": [{"code": "600001"}],
        "auction_scan_codes": ["sh600001", "sz000811", "sh600003"],
    }

    assert ac.auction_scan_codes(pool, full_universe=False) == ["sh600001"]
    assert ac.auction_scan_codes(pool, full_universe=True) == [
        "sh600001", "sz000811", "sh600003",
    ]


def test_auction_scan_codes_falls_back_when_weak_regime_empties_candidates():
    """弱市门禁把候选降级为 research_only 时 candidates 为空，不能扫 0 只股票。"""
    pool = {
        "candidates": [],
        "auction_scan_codes": ["sh600001", "sz000811", "sh600001"],
    }

    assert ac.auction_scan_codes(pool, full_universe=False) == ["sh600001", "sz000811"]


def test_auction_scan_codes_empty_pool_returns_empty():
    assert ac.auction_scan_codes({}, full_universe=False) == []
    assert ac.auction_scan_codes({}, full_universe=True) == []


def test_watch_pool_codes_includes_local_theme_members():
    """issue #260 B.1：D0 局部观察成员也要进 09:15-09:25 深池抓取，否则 09:25
    二次确认永远没有新鲜竞价证据。"""
    pool = {
        "execution_candidates": [{"code": "sh600001"}],
        "local_theme_candidates": [{"code": "sh600002"}, {"code": "sz000003"}],
    }

    assert ac.watch_pool_codes(pool) == ["sh600001", "sh600002", "sz000003"]


def test_watch_pool_codes_deduplicates_overlap_with_execution_candidates():
    pool = {
        "execution_candidates": [{"code": "sh600001"}],
        "local_theme_candidates": [{"code": "sh600001"}, {"code": "sh600002"}],
    }

    assert ac.watch_pool_codes(pool) == ["sh600001", "sh600002"]


def test_watch_pool_codes_ignores_plain_research_candidates():
    """普通 research_candidates 不在这条路径——只有 local_theme_candidates
    才能让研究票获得深池竞价抓取。"""
    pool = {
        "execution_candidates": [{"code": "sh600001"}],
        "research_candidates": [{"code": "sh600001"}, {"code": "sh699999"}],
    }

    assert ac.watch_pool_codes(pool) == ["sh600001"]


def test_full_universe_single_snapshot_keeps_pool_outsider_for_research(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        ac,
        "take_snapshot_with_failures",
        lambda codes, **kwargs: ({
            code: {
                "t": "09:24:50",
                "name": code,
                "price": 11.0,
                "prev_close": 10.0,
                "bids": [],
                "asks": [],
            }
            for code in codes
        }, {}),
    )

    state = ac.append_snapshot(["sh600001", "sz000811"], "2026-06-23")

    assert set(state["series"]) == {"sh600001", "sz000811"}


def test_full_universe_recall_collapses_real_series_to_latest_point():
    pool = {
        "prefilter_codes": [],
        "full_market_codes": ["600001"],
    }
    quotes = {
        "600001": [
            {"t": "09:15:00", "price": 10.0},
            {"t": "09:25:00", "price": 10.2},
        ],
    }

    annotated = ac.annotate_recall_snapshot(quotes, pool)

    assert annotated["600001"]["t"] == "09:25:00"
    assert annotated["600001"]["auction_series_count"] == 2



def test_append_snapshot_consumes_immutable_input_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        ac,
        "take_snapshot_with_failures",
        lambda codes, **kwargs: ({
            codes[0]: {
                "t": "09:20:00",
                "name": "测试股票",
                "price": 10.5,
                "prev_close": 10.0,
                "bids": [],
                "asks": [],
            }
        }, {}),
    )

    state = ac.append_snapshot(["sh600001"], "2026-06-12")

    assert state["input_snapshots"][-1]["snapshot_id"].startswith("snap-")
    assert state["series"]["sh600001"][0]["price"] == 10.5


def test_append_snapshot_marks_local_history_source_version(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        ac,
        "take_snapshot_with_failures",
        lambda codes, **kwargs: ({
            codes[0]: [{
                "t": "09:25:00", "price": 10.5, "prev_day_volume": 1000,
                "prev_day_provider": "local_history",
                "prev_day_source_version": "local-history-v1",
                "prev_day_provenance": {
                    "provider": "local_history", "dataset": "daily_bars",
                    "date": "2026-06-11", "source_version": "local-history-v1",
                },
            }]
        }, {}),
    )

    state = ac.append_snapshot(["sh600001"], "2026-06-12")
    snapshot = state["input_snapshots"][-1]

    assert snapshot["source_versions"]["previous_day_volume"] == "local-history-v1"


def test_finalize_persists_dynamic_shortlist_and_lifecycle(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试
    # 状态无条件设置了它；这里要用 HERMES_HOME 驱动每个用例独立的
    # tmp_path，必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ac.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(ac.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(ac.signal_ledger, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(ac, "scan_many", lambda codes: {str(code)[-6:]: [] for code in codes})
    monkeypatch.setattr(
        ac,
        "read_market_context",
        lambda: {
            "status": "ok",
            "context_status": "fresh",
            "context_fresh": True,
            "sector_impact": {},
            "alerts": [],
        },
    )
    atomic_write_json(
        str(tmp_path / "skills" / "stock-triage" / "data" / "portfolio.json"),
        {"cash": 20000, "positions": [], "cash_reconciled": True},
    )
    source_asof = "2026-06-10"
    event_asof = "2026-06-11"
    candidates = [
        {
            "code": f"600{i:03d}",
            "name": f"股票{i}",
            "sector": "半导体",
            "daban_score": 90 - i,
            "trend_score": 70 - i,
            "selected_by": {"daban": True, "trend": False},
        }
        for i in range(8)
    ]
    pool = {
        "status": "ready",
        "asof": source_asof,
        "candidates": candidates,
    }
    atomic_write_json(ac._pool_path(), pool)
    candidate_lifecycle.initialize_day(source_asof, candidates)
    series = {
        f"sh{item['code']}": [{
            "t": "09:24:50",
            "name": item["name"],
            "price": 10.5,
            "prev_close": 10.0,
            "volume": 20_000 - index * 100,
            "matched": 2_000_000, "unmatched": 0,
            "prev_day_volume": 1_000_000,
            "prev_day_amount": 100_000_000,
            "market_cap": 100.0,
            "bids": [(10.5, 5_000)] * 5,
            "asks": [(10.51, 2_000)] * 5,
        }]
        for index, item in enumerate(candidates)
    }
    atomic_write_json(ac._state_path(event_asof), {"asof": event_asof, "series": series})

    result = ac.finalize(event_asof, shortlist_limit=5)

    assert result["schema"] == "auction_finalize_v2"
    assert len(result["shortlist"]) == 5
    assert len(result["preopen_decisions"]) == 5
    assert all(item["execution_plan"]["same_day_sell_allowed"] is False for item in result["preopen_decisions"])
    assert read_json(ac._shortlist_path(event_asof), {})["asof"] == event_asof
    monitors = ac.monitor_registry.active_entries("stock", asof=event_asof)
    assert len(monitors) == 5
    assert {item["source_group"] for item in monitors} == {"auction_shortlist"}
    lifecycle = candidate_lifecycle.load_day(source_asof)
    assert sum(record["current_stage"] == "auction_shortlist" for record in lifecycle["records"]) == 5


def test_finalize_flags_empty_collection_instead_of_reporting_ready(tmp_path, monkeypatch):
    """采集到 0 只股票时 finalize 不能报 ready，否则下游把空结果当成有效观测。"""
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ac.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(ac.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(ac.signal_ledger, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(ac, "scan_many", lambda codes: {str(code)[-6:]: [] for code in codes})
    monkeypatch.setattr(
        ac,
        "read_market_context",
        lambda: {
            "status": "ok",
            "context_status": "fresh",
            "context_fresh": True,
            "sector_impact": {},
            "alerts": [],
        },
    )
    atomic_write_json(
        str(tmp_path / "skills" / "stock-triage" / "data" / "portfolio.json"),
        {"cash": 20000, "positions": [], "cash_reconciled": True},
    )
    source_asof = "2026-06-10"
    event_asof = "2026-06-11"
    candidates = [{
        "code": "600001",
        "name": "股票1",
        "sector": "半导体",
        "daban_score": 90,
        "trend_score": 70,
        "selected_by": {"daban": True, "trend": False},
    }]
    atomic_write_json(ac._pool_path(), {
        "status": "ready",
        "asof": source_asof,
        "candidates": candidates,
    })
    candidate_lifecycle.initialize_day(source_asof, candidates)
    # 竞价采集彻底失败：series 为空
    atomic_write_json(ac._state_path(event_asof), {"asof": event_asof, "series": {}})

    result = ac.finalize(event_asof, shortlist_limit=5)

    assert result["status"] == "degraded"
    assert result["collection_status"] == "empty"
    assert result["factor_count"] == 0
    assert result["research_only"] is True
    assert result["preopen_decisions"] == []
    assert any("竞价采集为空" in note for note in result.get("degraded_reasons") or [])
    # 空采集不得注册监控，否则下游把空观测当成有效标的
    assert ac.monitor_registry.active_entries("stock", asof=event_asof) == []


def test_finalize_marks_research_only_when_delivery_gate_clears_the_pool(tmp_path, monkeypatch):
    """采集正常但弱市交付门禁清零候选池时，也必须标 research_only。

    这条区分的是"无机会"（采集到了、但没有一只够格）与"无观测"（采集失败）。
    此前 research_only 恒为 False，空短名单会被下游当成"已评估、可执行"的结论。
    """
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ac.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(ac.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(ac.signal_ledger, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(ac, "scan_many", lambda codes: {str(code)[-6:]: [] for code in codes})
    monkeypatch.setattr(
        ac,
        "read_market_context",
        lambda: {
            "status": "ok", "context_status": "fresh", "context_fresh": True,
            "sector_impact": {}, "alerts": [],
        },
    )
    # 弱市：没有任何候选通过交付门禁
    monkeypatch.setattr(
        ac.candidate_pipeline,
        "assess_delivery_quality",
        lambda item, *, lane, stage, selection_state=None: {
            "status": "research_only", "reasons": ["弱市交付门禁未通过"],
        },
    )
    atomic_write_json(
        str(tmp_path / "skills" / "stock-triage" / "data" / "portfolio.json"),
        {"cash": 20000, "positions": [], "cash_reconciled": True},
    )
    source_asof = "2026-06-10"
    event_asof = "2026-06-11"
    candidates = [
        {
            "code": f"600{i:03d}", "name": f"股票{i}", "sector": "银行",
            "daban_score": 90 - i, "trend_score": 70 - i,
            "selected_by": {"daban": True, "trend": False},
        }
        for i in range(8)
    ]
    atomic_write_json(ac._pool_path(), {
        "status": "ready",
        "asof": source_asof,
        "candidates": [],
        "research_candidates": candidates,
        "execution_candidates": [],
        "auction_scan_universe": [f"sh{item['code']}" for item in candidates],
        "auction_scan_codes": [f"sh{item['code']}" for item in candidates],
        "counts": {"research": 8, "execution": 0, "auction_scan": 8},
        "gate": {"status": "weak_market", "reasons": ["弱市交付门禁未通过"]},
    })
    candidate_lifecycle.initialize_day(source_asof, candidates)
    atomic_write_json(ac._state_path(event_asof), {"asof": event_asof, "series": {
        f"sh{item['code']}": [{
            "t": "09:24:50", "name": item["name"], "price": 10.5, "prev_close": 10.0,
            "volume": 20_000 - index * 100, "market_cap": 100.0,
            "matched": 2_000_000, "unmatched": 0,
            "prev_day_volume": 1_000_000, "prev_day_amount": 100_000_000,
            "bids": [(10.5, 5_000)] * 5, "asks": [(10.51, 2_000)] * 5,
        }]
        for index, item in enumerate(candidates)
    }})

    result = ac.finalize(event_asof, shortlist_limit=5)

    # 采集是成功的（8 只全部有观测并被逐项判定），但门禁清零了池子
    assert result["status"] == "ready"
    assert result["outcome_status"] == "ok_research_only"
    assert result["input_count"] == 8
    assert result["research_count"] == 8
    assert result["execution_count"] == 0
    # 8 只全部落进 rejected（同一只可能被 fill 循环与末尾扫描各记一次，故按代码去重）
    assert len({str(item["code"])[-6:] for item in result["rejected"]}) == 8
    assert result["shortlist_count"] == 0
    assert result["shortlist"] == []
    assert len(result["research_candidates"]) == 8
    assert result["research_candidates"][0]["research_only"] is True
    assert result["execution_candidates"] == []
    assert result["research_only"] is True
    assert result["preopen_decisions"] == []
    # 没有可交付候选就不得注册监控
    assert ac.monitor_registry.active_entries("stock", asof=event_asof) == []


def test_finalize_degrades_when_the_snapshot_job_never_ran(tmp_path, monkeypatch):
    """依赖门放行失败的上游后，finalize 必须自己判空降级，而不是抛异常或报结论。

    auction-finalize 的 dependency_policy 接受 timeout/failed 的 auction-snapshot，
    因此 finalize 会在"当日竞价状态文件根本不存在、观察池也没有"的裸状态下被调起。
    此时唯一可接受的产出是 fail-closed 的降级报告。
    """
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ac.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(ac.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(ac.signal_ledger, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    event_asof = "2026-06-11"

    result = ac.finalize(event_asof, shortlist_limit=5)

    assert result["status"] == "degraded"
    assert result["collection_status"] == "empty"
    assert result["research_only"] is True
    assert result["shortlist"] == []
    assert result["preopen_decisions"] == []
    assert ac.monitor_registry.active_entries("stock", asof=event_asof) == []
    # 报告层同样不得把"没有观测"渲染成"没有机会"
    report = ac.json_report(result)
    assert report["status"] == "degraded"
    assert report["research_only"] is True
    assert report["decision_count"] == 0
    assert report["degraded_reasons"]


def test_json_report_passes_through_research_only_instead_of_hardcoding_false():
    """research_only 曾被硬编码 False，使降级报告自相矛盾（status=degraded 却 research_only=False）。"""
    degraded = {
        "schema": "auction_finalize_v2",
        "asof": "2026-07-20",
        "status": "degraded",
        "collection_status": "empty",
        "research_only": True,
        "degraded_reasons": ["竞价采集为空（0 只标的），无盘中观测，拒绝输出可执行结论"],
        "input_count": 0,
        "shortlist": [],
        "preopen_decisions": [],
    }

    report = ac.json_report(degraded)

    assert report["status"] == "degraded"
    assert report["research_only"] is True
    assert report["degraded_reasons"] == degraded["degraded_reasons"]
    assert report["score_is_probability"] is False
    assert "非涨停概率" in report["score_label"]
    # 正常结果仍应是 False
    assert ac.json_report({"status": "ready", "shortlist": []})["research_only"] is False


def test_json_report_exposes_nonordinary_business_outcome_for_zero_input():
    report = ac.json_report({
        "schema": "auction_finalize_v2",
        "status": "ready",
        "outcome_status": "ok_no_actionable_candidates",
        "reason_code": "no_research_or_execution_candidates",
        "input_count": 0,
        "research_count": 0,
        "execution_count": 0,
        "auction_scan_count": 200,
        "shortlist": [],
        "research_candidates": [],
        "execution_candidates": [],
        "preopen_decisions": [],
    })

    assert report["outcome_status"] == "ok_no_actionable_candidates"
    assert report["reason_code"] == "no_research_or_execution_candidates"
    assert report["input_count"] == 0


def test_optional_market_snapshot_failure_is_explicit_but_does_not_block_core_finalize(
    monkeypatch,
):
    monkeypatch.setenv(
        "HERMES_CONTEXT_FROM",
        json.dumps([
            {"job_id": "auction-snapshot", "status": "ok", "gate_status": "passed"},
            {
                "job_id": "auction-market-snapshot",
                "status": "timeout",
                "gate_status": "passed",
                "reasons": [],
            },
        ]),
    )

    status = ac.optional_market_intelligence_status()

    assert status["status"] == "market_intelligence_degraded"
    assert status["degraded"] is True
    assert status["upstream_status"] == "timeout"
    assert status["reasons"] == ["status_timeout"]


def test_finalize_preserves_mainline_strategy_attribution(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试
    # 状态无条件设置了它；这里要用 HERMES_HOME 驱动每个用例独立的
    # tmp_path，必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ac.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(ac.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(ac.signal_ledger, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(ac, "scan_many", lambda codes: {str(code)[-6:]: [] for code in codes})
    monkeypatch.setattr(ac.strategy_registry, "live_record", lambda _strategy_id: None)
    atomic_write_json(
        str(tmp_path / "skills" / "stock-triage" / "data" / "portfolio.json"),
        {"cash": 20000, "positions": [], "cash_reconciled": True},
    )
    source_asof = "2026-06-10"
    event_asof = "2026-06-11"
    candidate = {
        "code": "600001",
        "name": "主线龙头",
        "sector": "半导体",
        "daban_score": 95,
        "trend_score": 20,
        "hot_money_qualified": True,
        "selected_by": {"daban": True, "trend": False},
        "selection_context": {
            "window": "D0_close",
            "market_timing": {
                "reflexivity": {
                    "phase": "distribution",
                    "defensive_guards": ["leader_isolation_exit_v1"],
                    "risk_multiplier": 0.0,
                }
            },
        },
    }
    atomic_write_json(ac._pool_path(), {
        "status": "ready",
        "asof": source_asof,
        "candidates": [candidate],
    })
    candidate_lifecycle.initialize_day(source_asof, [candidate])
    atomic_write_json(ac._state_path(event_asof), {
        "asof": event_asof,
        "series": {
            "sh600001": [{
                "t": "09:24:50",
                "name": "主线龙头",
                "price": 10.5,
                "prev_close": 10.0,
                "volume": 20_000,
                "matched": 2_000_000, "unmatched": 0,
                "prev_day_volume": 1_000_000,
                "prev_day_amount": 100_000_000,
                "market_cap": 100.0,
                "bids": [(10.5, 5_000)] * 5,
                "asks": [(10.51, 2_000)] * 5,
            }],
        },
    })

    result = ac.finalize(event_asof, shortlist_limit=1)

    decision = result["preopen_decisions"][0]
    assert decision["strategy_id"] == "daban:mainline_leader_confirm"
    assert decision["selection_context"]["window"] == "09:25"
    assert decision["policy_decision"]["decision"] == "watch"
    report = ac.json_report(result)
    assert report["research_only"] is False
    assert report["top_candidates"][0]["sector"] == "半导体"
    assert report["top_candidates"][0]["strategy_id"] == "daban:mainline_leader_confirm"
    assert "factors" not in report
    lifecycle = candidate_lifecycle.load_day(source_asof)
    event = lifecycle["records"][0]["stage_history"][-1]
    assert event["details"]["reflexivity"]["phase"] == "distribution"
    assert "policy_reasons" in event["details"]


def test_finalize_passes_selection_market_risk_to_policy(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试
    # 状态无条件设置了它；这里要用 HERMES_HOME 驱动每个用例独立的
    # tmp_path，必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ac.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(ac.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(ac.signal_ledger, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(ac, "scan_many", lambda codes: {str(code)[-6:]: [] for code in codes})
    captured = []

    def _policy(**kwargs):
        captured.append(kwargs.get("market_crowding"))
        return {
            "decision": "watch",
            "position_multiplier": 0.0,
            "requested_action": kwargs["requested_action"],
            "reasons": ["test"],
        }

    monkeypatch.setattr(ac, "evaluate_decision", _policy)
    source_asof = "2026-06-10"
    event_asof = "2026-06-11"
    candidate = {
        "code": "600001",
        "name": "退潮候选",
        "daban_score": 95,
        "trend_score": 20,
        "hot_money_qualified": True,
        "selected_by": {"daban": True, "trend": False},
        "selection_context": {
            "window": "D0_close",
            "market_timing": {"dominant_state": "S6", "fragility_score": 0.8},
        },
    }
    atomic_write_json(ac._pool_path(), {
        "status": "ready",
        "asof": source_asof,
        "candidates": [candidate],
    })
    candidate_lifecycle.initialize_day(source_asof, [candidate])
    atomic_write_json(ac._state_path(event_asof), {
        "asof": event_asof,
        "series": {
            "sh600001": [{
                "t": "09:24:50",
                "name": "退潮候选",
                "price": 10.5,
                "prev_close": 10.0,
                "volume": 20_000,
                "matched": 2_000_000, "unmatched": 0,
                "prev_day_volume": 1_000_000,
                "prev_day_amount": 100_000_000,
                "market_cap": 100.0,
                "bids": [(10.5, 5_000)] * 5,
                "asks": [(10.51, 2_000)] * 5,
            }],
        },
    })

    ac.finalize(event_asof, shortlist_limit=1)

    assert captured == [{"dominant_state": "S6", "fragility_score": 0.8}]


def test_finalize_computes_real_discipline_state_from_ledger(tmp_path, monkeypatch):
    """market_gate 阈值(config/daban_thresholds.yaml)必须对照真实交易记录，
    而不是永远为0的默认值——否则'连续错单3次冻结交易'从未真正生效过。"""
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试
    # 状态无条件设置了它；这里要用 HERMES_HOME 驱动每个用例独立的
    # tmp_path，必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ac.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(ac.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(ac.signal_ledger, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(ac, "scan_many", lambda codes: {str(code)[-6:]: [] for code in codes})
    atomic_write_json(
        str(tmp_path / "skills" / "stock-triage" / "data" / "portfolio.json"),
        {"cash": 100000, "positions": [], "cash_reconciled": True},
    )
    for trade_date in ["2026-06-08", "2026-06-09", "2026-06-10"]:
        ac.signal_ledger.append_event(
            "trade.executed",
            ac.signal_ledger.make_links(f"loss-{trade_date}"),
            {"code": "600001", "action": "close", "trade_date": trade_date, "pnl": -100, "pnl_pct": -3.0},
        )
    source_asof = "2026-06-10"
    event_asof = "2026-06-11"
    candidate = {
        "code": "600002",
        "name": "候选票",
        "daban_score": 90,
        "trend_score": 20,
        "hot_money_qualified": True,
        "selected_by": {"daban": True, "trend": False},
        "selection_context": {"window": "D0_close"},
    }
    atomic_write_json(ac._pool_path(), {"status": "ready", "asof": source_asof, "candidates": [candidate]})
    candidate_lifecycle.initialize_day(source_asof, [candidate])
    atomic_write_json(ac._state_path(event_asof), {
        "asof": event_asof,
        "series": {
            "sh600002": [{
                "t": "09:24:50", "name": "候选票", "price": 10.5, "prev_close": 10.0,
                "volume": 20_000, "market_cap": 100.0,
                "matched": 2_000_000, "unmatched": 0,
                "prev_day_volume": 1_000_000, "prev_day_amount": 100_000_000,
                "bids": [(10.5, 5_000)] * 5, "asks": [(10.51, 2_000)] * 5,
            }],
        },
    })

    result = ac.finalize(event_asof, shortlist_limit=1)

    assert result["discipline_state"]["consecutive_losses"] == 3
    assert result["discipline_state"]["blocked"] is True
    assert "consecutive_losses_freeze" in result["discipline_state"]["reasons"]
    decision = result["preopen_decisions"][0]
    assert decision["policy_decision"]["decision"] == "avoid"
    assert "consecutive_losses_freeze" in decision["policy_decision"]["reasons"]
    report = ac.json_report(result)
    assert report["discipline_state"]["blocked"] is True


def test_limit_down_auction_is_flagged():
    """issue #139 贤丰控股(002141)：昨收涨停后竞价崩到跌停，必须打上跌停标记。"""
    snaps = [{
        "t": "09:25:00", "name": "贤丰控股", "price": 5.67, "prev_close": 6.30,
        "volume": 8_000, "market_cap": 60.0,
        "bids": [(5.67, 100)] + [(None, None)] * 4,
        "asks": [(5.67, 900_000)] + [(None, None)] * 4,
    }]
    f = ac.compute_auction_factors(snaps, "sz002141", "贤丰控股")
    assert f["limit_down"] == 5.67
    assert f["board_status"] == "limit_down"
    assert f["is_limit_down"] is True


def test_auction_price_decay_tracks_indicative_price_fade():
    """issue #139：+9.52% → +3.02% 的竞价回落必须被量化成 decay。"""
    base = {"name": "贤丰控股", "prev_close": 6.30, "volume": 0,
            "market_cap": 60.0, "bids": [(6.49, 1000)] * 5, "asks": [(6.50, 1000)] * 5}
    snaps = [
        {**base, "t": "09:16:00", "price": 6.90},
        {**base, "t": "09:20:00", "price": 6.68},
        {**base, "t": "09:25:00", "price": 6.49},
    ]
    f = ac.compute_auction_factors(snaps, "sz002141", "贤丰控股")
    assert f["auction_gap_pct"] == 3.02
    assert f["auction_max_gap_pct"] == 9.52
    assert f["auction_price_decay_pct"] == 6.5
    assert f["board_status"] == "high_open"


def test_auction_fade_from_limit_up_to_flat_is_detected():
    """issue #140 天融信(002212)：竞价 +10% 涨停价一路回落到 0% 平开。"""
    base = {"name": "天融信", "prev_close": 6.60, "volume": 0,
            "market_cap": 78.0, "bids": [(6.60, 39263)] * 5, "asks": [(6.60, 39263)] * 5}
    snaps = [
        {**base, "t": "09:16:00", "price": 7.26},
        {**base, "t": "09:20:00", "price": 7.25},
        {**base, "t": "09:23:00", "price": 6.98},
        {**base, "t": "09:25:00", "price": 6.60},
    ]
    f = ac.compute_auction_factors(snaps, "sz002212", "天融信")
    assert f["auction_gap_pct"] == 0.0
    assert f["auction_max_gap_pct"] == 10.0
    assert f["auction_price_decay_pct"] == 10.0
    assert f["auction_faded_from_limit_up"] is True
    assert f["board_status"] == "flat_or_low_open"
    assert f["is_limit_down"] is False


def test_zero_volume_and_mirrored_book_marked_degraded():
    """issue #140 免费源局限：竞价量能恒 0、五档买卖完全相等 → 数据质量降级。"""
    base = {"name": "天融信", "prev_close": 6.60, "volume": 0, "market_cap": 78.0}
    snaps = [
        {**base, "t": "09:20:00", "price": 7.25,
         "bids": [(7.25, 16988)] * 5, "asks": [(7.25, 16988)] * 5},
        {**base, "t": "09:25:00", "price": 6.60,
         "bids": [(6.60, 39263)] * 5, "asks": [(6.60, 39263)] * 5},
    ]
    f = ac.compute_auction_factors(snaps, "sz002212", "天融信")
    assert f["auction_data_quality"] == "degraded"
    notes = " ".join(f["auction_data_quality_notes"])
    assert "量能" in notes and "五档" in notes


def test_real_book_is_not_degraded():
    snaps = [{
        "t": "09:25:00", "name": "北方稀土", "price": 21.5, "prev_close": 20.0,
        "volume": 30000, "market_cap": 300.0,
        "bids": [(21.5, 4000)] * 5, "asks": [(21.51, 2000)] * 5,
    }]
    f = ac.compute_auction_factors(snaps, "sh600111", "北方稀土")
    assert f["auction_data_quality"] == "ok"
    assert f["auction_data_quality_notes"] == []


def test_snapshot_failures_reach_the_artifact_instead_of_being_dropped(tmp_path, monkeypatch):
    """provider 逐股记了失败原因，采集器过去用 `_failures` 原地丢掉了。

    结果是 artifact 只有「采到几只」，没有「剩下的怎么了」——竞价窗口出问题时
    根本无法区分「池子本来就小」和「数据源挂了」。
    """
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        ac,
        "take_snapshot_with_failures",
        lambda codes, **kwargs: (
            {"600001": [{"t": "09:20:00", "price": 10.0}]},
            {"000811": "easy_tdx 0x123D 无有效 09:15-09:25 竞价数据"},
        ),
    )

    state = ac.append_snapshot(["sh600001", "sz000811"], "2026-06-23")

    summary = state["snapshot_failures"]
    assert summary["total"] == 1
    assert summary["by_reason"][0]["count"] == 1
    assert summary["by_reason"][0]["sample_codes"] == ["000811"]
    assert "easy_tdx" in summary["by_reason"][0]["reason"]


def test_snapshot_failure_summary_stays_bounded_for_a_full_pool():
    """500 只全挂时 artifact 不能变成 500 行；按原因聚合 + 少量样本码。"""
    failures = {f"{600000 + i:06d}": "budget_exhausted: 竞价抓取超出 144s 预算" for i in range(500)}
    failures["000001"] = "easy_tdx 0x123D 无有效 09:15-09:25 竞价数据"

    summary = ac.summarize_snapshot_failures(failures)

    assert summary["total"] == 501
    assert len(summary["by_reason"]) == 2
    # 最大的一组排前面，样本码有界
    assert summary["by_reason"][0]["count"] == 500
    assert len(summary["by_reason"][0]["sample_codes"]) == 3


def test_fetch_budget_is_derived_from_the_job_timeout(tmp_path, monkeypatch):
    """预算跟着 manifest 的 run.timeout_seconds 走，避免两处数字各自漂移。"""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("A_STOCK_JOB_TIMEOUT_SECONDS", "180")
    seen = {}

    def _capture(codes, **kwargs):
        seen.update(kwargs)
        return {"600001": [{"t": "09:20:00", "price": 10.0}]}, {}

    monkeypatch.setattr(ac, "take_snapshot_with_failures", _capture)

    ac.append_snapshot(["sh600001"], "2026-06-23")

    assert seen["deadline_seconds"] == 144.0


def test_fetch_budget_is_absent_when_the_runner_declares_no_timeout(tmp_path, monkeypatch):
    """手工跑（没有 runner 注入超时）时不应凭空造一个预算。"""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("A_STOCK_JOB_TIMEOUT_SECONDS", raising=False)
    seen = {}

    def _capture(codes, **kwargs):
        seen.update(kwargs)
        return {"600001": [{"t": "09:20:00", "price": 10.0}]}, {}

    monkeypatch.setattr(ac, "take_snapshot_with_failures", _capture)

    ac.append_snapshot(["sh600001"], "2026-06-23")

    assert seen["deadline_seconds"] is None


def test_finalize_carries_snapshot_failures_into_the_report(tmp_path, monkeypatch):
    """09:26 看到 shortlist 很短时，必须能当场区分「池子小」和「数据源挂了」。"""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        ac,
        "take_snapshot_with_failures",
        lambda codes, **kwargs: (
            {},
            {"000811": "budget_exhausted: 竞价抓取超出 144s 预算，该标的未取数"},
        ),
    )
    ac.append_snapshot(["sz000811"], "2026-06-23")

    result = ac.finalize("2026-06-23")
    report = ac.json_report(result)

    assert result["status"] == "degraded"
    assert report["snapshot_failures"]["total"] == 1
    assert "budget_exhausted" in report["snapshot_failures"]["by_reason"][0]["reason"]


def _seed_pool(tmp_path, monkeypatch, *, pool_asof, codes=("sh600001",)):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    atomic_write_json(ac._pool_path(), {
        "status": "ready",
        "asof": pool_asof,
        "candidates": [
            {"code": code, "name": code, "volume": 1000, "amount": 20000}
            for code in codes
        ],
    })


def test_stale_watch_pool_is_marked_on_the_snapshot(tmp_path, monkeypatch):
    """candidate-preopen 挂掉时观察池会停在昨天，但此前没有任何地方记下这件事。

    candidate_pool_latest.json 是一个 latest 文件，load_watch_pool 本来就容忍
    MAX_POOL_AGE_DAYS 天 —— 也就是说隔夜池会被当成正常池静默使用。
    """
    _seed_pool(tmp_path, monkeypatch, pool_asof="2026-06-22")
    monkeypatch.setattr(
        ac,
        "take_snapshot_with_failures",
        lambda codes, **kwargs: ({"600001": [{"t": "09:20:00", "price": 10.0}]}, {}),
    )

    state = ac.append_snapshot(["sh600001"], "2026-06-23")

    assert state["pool_stale"]["stale"] is True
    assert state["pool_stale"]["pool_asof"] == "2026-06-22"
    assert state["pool_stale"]["age_days"] == 1


def test_fresh_watch_pool_is_not_marked_stale(tmp_path, monkeypatch):
    _seed_pool(tmp_path, monkeypatch, pool_asof="2026-06-23")
    monkeypatch.setattr(
        ac,
        "take_snapshot_with_failures",
        lambda codes, **kwargs: ({"600001": [{"t": "09:20:00", "price": 10.0}]}, {}),
    )

    state = ac.append_snapshot(["sh600001"], "2026-06-23")

    assert state["pool_stale"]["stale"] is False
    assert state["pool_stale"]["age_days"] == 0


def test_stale_pool_forces_research_only_so_it_never_reaches_execution(tmp_path, monkeypatch):
    """隔夜池可以用来观测，但不能用来下单。

    open_confirmation 已有 fail-closed 安全网：shortlist_result.research_only 为真
    时信号一律清零。所以这里只需保证 stale 会把它置真，并写明原因。
    """
    _seed_pool(tmp_path, monkeypatch, pool_asof="2026-06-22")
    monkeypatch.setattr(
        ac,
        "take_snapshot_with_failures",
        lambda codes, **kwargs: ({"600001": [{
            "t": "09:25:00", "name": "示例", "price": 11.0, "prev_close": 10.0,
            "volume": 12000, "market_cap": 80.0,
            "matched": 1200000, "unmatched": 0,
            "prev_day_volume": 100000.0,
            "bids": [(11.0, 90000)] + [(None, None)] * 4,
            "asks": [(None, None)] * 5,
        }]}, {}),
    )
    ac.append_snapshot(["sh600001"], "2026-06-23")

    result = ac.finalize("2026-06-23")
    report = ac.json_report(result)

    assert result["research_only"] is True
    assert report["research_only"] is True
    assert report["pool_stale"]["stale"] is True
    assert any("隔夜" in reason or "过期" in reason for reason in report["degraded_reasons"])
