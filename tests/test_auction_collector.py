"""集合竞价采集器 — 五档解析 + 真竞价因子计算（纯函数，不触网）"""

import importlib.util
from pathlib import Path

from a_stock_http import parse_tencent_orderbook_line, _TENCENT_BID_BASE, _TENCENT_ASK_BASE


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
