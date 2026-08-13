"""指数趋势闸门（P1，shadow only）测试。"""

import index_trend_gate as itg

_CFG = {
    "enabled": True,
    "index_code": "000001",
    "index_market": "sh",
    "ma_periods": [5, 10, 20],
    "volume_shrink_ratio": 0.7,
    "defend_below_ma": 20,
    "reduce_below_ma": 5,
    "min_bars": 21,
}


def _bars(closes, volumes):
    return [
        {"date": f"2026-06-{i + 1:02d}", "open": c, "close": c, "high": c,
         "low": c, "volume": v}
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


def test_healthy_uptrend_triggers_no_shadow_action():
    closes = [10 + i * 0.1 for i in range(25)]       # 单调上行，收盘在所有均线之上
    volumes = [1_000_000] * 25
    result = itg.assess_index_trend(_bars(closes, volumes), config=_CFG)
    assert result["available"] is True
    assert result["would_reduce"] is False
    assert result["would_defend"] is False
    assert result["reasons"] == []


def test_break_below_ma20_triggers_defend():
    # 前高后崩：最后一根收盘远低于 20 日线
    closes = [20.0] * 20 + [19, 18, 17, 16, 12.0]
    volumes = [1_000_000] * 25
    result = itg.assess_index_trend(_bars(closes, volumes), config=_CFG)
    assert result["available"] is True
    assert result["below_ma"]["20"] is True
    assert result["would_defend"] is True
    assert any("20日线" in r for r in result["reasons"])


def test_break_below_ma5_only_triggers_reduce_not_defend():
    # 站在 20 日线上方但跌破 5 日线：只减仓不防守
    closes = [10 + i * 0.2 for i in range(24)] + [14.0]
    # 24 根到 ~14.6，MA20 ≈ 12.9（在下方），MA5 用最后 5 根 ~14.9 → 14.0<MA5 且 >MA20
    volumes = [1_000_000] * 25
    result = itg.assess_index_trend(_bars(closes, volumes), config=_CFG)
    assert result["available"] is True
    assert result["below_ma"]["5"] is True
    assert result["below_ma"]["20"] is False
    assert result["would_reduce"] is True
    assert result["would_defend"] is False


def test_volume_shrink_flagged():
    closes = [10 + i * 0.1 for i in range(25)]
    volumes = [1_000_000] * 23 + [400_000, 400_000]   # 近两日缩量
    result = itg.assess_index_trend(_bars(closes, volumes), config=_CFG)
    assert result["volume_shrink"] is True
    assert any("均量" in r for r in result["reasons"])


def test_insufficient_bars_fails_closed():
    closes = [10.0] * 10
    volumes = [1_000_000] * 10
    result = itg.assess_index_trend(_bars(closes, volumes), config=_CFG)
    assert result["available"] is False
    assert result["would_reduce"] is False
    assert result["would_defend"] is False
    assert "fail-closed" in result["reason"]


def test_fetch_uses_injected_fetcher_and_stays_offline():
    closes = [10 + i * 0.1 for i in range(25)]
    volumes = [1_000_000] * 25
    calls = {}

    def fake_fetcher(code, market="sh", days=40):
        calls["code"] = code
        calls["market"] = market
        return _bars(closes, volumes)

    result = itg.fetch_index_trend(config=_CFG, fetcher=fake_fetcher)
    assert calls["code"] == "000001"
    assert calls["market"] == "sh"
    assert result["available"] is True


def test_fetch_failure_fails_closed():
    from a_stock_http import DataSourceError

    def boom(code, market="sh", days=40):
        raise DataSourceError("tencent", "network down")

    result = itg.fetch_index_trend(config=_CFG, fetcher=boom)
    assert result["available"] is False
    assert "取数失败" in result["reason"]
