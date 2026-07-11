"""集合竞价采集器 — 五档解析 + 真竞价因子计算（纯函数，不触网）"""

import importlib.util
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


def test_yiziban_detected_and_seal_ratio():
    snaps = [{
        "t": "09:24:50", "name": "通富微电", "price": 11.0, "prev_close": 10.0,
        "volume": 12000, "market_cap": 80.0,
        "bids": [(11.0, 90000)] + [(None, None)] * 4,
        "asks": [(None, None)] * 5,
    }]
    f = ac.compute_auction_factors(snaps, "sz002156", "通富微电")
    assert f["auction_gap_pct"] == 10.0
    assert f["board_status"] == "yizi_seal"
    assert f["is_yiziban"] is True
    # 封单额/流通市值 = 90000手*100*11 / (80亿) *100
    assert f["seal_amount_ratio_pct"] == round(90000 * 100 * 11.0 / (80.0e8) * 100, 3)


def test_high_open_bid_ask_ratio():
    snaps = [{
        "t": "09:20:00", "name": "北方稀土", "price": 21.5, "prev_close": 20.0,
        "volume": 30000, "market_cap": 300.0,
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
        "bids": [(11.0, 90000)] + [(None, None)] * 4, "asks": [(None, None)] * 5,
    }]}
    r = ac._build_result(series, "2026-06-04")
    assert r["schema"] == "auction_factors_v1"
    assert len(r["factors"]) == 1
    assert "chanlun-backtest" in r["note"]


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


def test_full_universe_single_snapshot_keeps_pool_outsider_for_research(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        ac,
        "take_snapshot",
        lambda codes: {
            code: {
                "t": "09:24:50",
                "name": code,
                "price": 11.0,
                "prev_close": 10.0,
                "bids": [],
                "asks": [],
            }
            for code in codes
        },
    )

    state = ac.append_snapshot(["sh600001", "sz000811"], "2026-06-23")

    assert set(state["series"]) == {"sh600001", "sz000811"}



def test_append_snapshot_consumes_immutable_input_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        ac,
        "take_snapshot",
        lambda codes: {
            codes[0]: {
                "t": "09:20:00",
                "name": "测试股票",
                "price": 10.5,
                "prev_close": 10.0,
                "bids": [],
                "asks": [],
            }
        },
    )

    state = ac.append_snapshot(["sh600001"], "2026-06-12")

    assert state["input_snapshots"][-1]["snapshot_id"].startswith("snap-")
    assert state["series"]["sh600001"][0]["price"] == 10.5


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
        "selection_context": {"window": "D0_close"},
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
