"""东财K线回退源 — PR #90 引用了 fetch_eastmoney_kline 但从未实现，导致
candidate_discovery import 失败、测试收集中断、CI 静默失效。本文件锁死该接口。"""

import market_adapters as ma


def test_parse_eastmoney_kline_payload_matches_tencent_bar_shape():
    payload = {
        "data": {
            "klines": [
                "2026-07-08,10.00,10.20,10.30,9.90,123456",
                "2026-07-09,10.20,10.50,10.60,10.10,234567",
                "bad-row",
            ],
        },
    }
    bars = ma.parse_eastmoney_kline_payload(payload, days=70)
    assert bars == [
        {"date": "2026-07-08", "open": 10.0, "close": 10.2,
         "high": 10.3, "low": 9.9, "volume": 123456.0},
        {"date": "2026-07-09", "open": 10.2, "close": 10.5,
         "high": 10.6, "low": 10.1, "volume": 234567.0},
    ]


def test_parse_eastmoney_kline_respects_days_window():
    payload = {"data": {"klines": [
        f"2026-07-0{i},1,1,1,1,{i}" for i in range(1, 8)
    ]}}
    bars = ma.parse_eastmoney_kline_payload(payload, days=3)
    assert len(bars) == 3
    assert bars[0]["date"] == "2026-07-05"


def test_parse_eastmoney_kline_empty_payload():
    assert ma.parse_eastmoney_kline_payload({}, days=10) == []
    assert ma.parse_eastmoney_kline_payload(None, days=10) == []


def test_candidate_discovery_import_contract():
    # 回归锁：candidate_discovery 顶层 import 的三个 adapter 必须存在
    assert callable(ma.fetch_eastmoney_kline)
    assert callable(ma.fetch_tencent_kline)
    assert callable(ma.fetch_tencent_quote)
