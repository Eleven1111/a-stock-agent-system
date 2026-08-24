import a_stock_http
import local_market_history
import market_adapters
from datetime import date, timedelta


def _local_bar(trading_date, close=10.0):
    return {
        "code": "600519",
        "trading_date": trading_date,
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "volume": 1000,
    }


def _tencent_response(key="qfqday"):
    return {
        "data": {
            "sh600519": {
                key: [
                    ["2026-08-17", "10", "10.2", "10.3", "9.9", "100"],
                    ["2026-08-18", "10.2", "10.4", "10.5", "10.1", "110"],
                ]
            }
        }
    }


def test_daily_kline_uses_local_history_without_network(monkeypatch):
    calls = []
    today = date.today()
    monkeypatch.setattr(
        local_market_history,
        "get_daily_bars",
        lambda codes, end_date, lookback: [
            _local_bar((today - timedelta(days=1)).isoformat()),
            _local_bar(today.isoformat(), 10.2),
        ],
    )
    monkeypatch.setattr(
        a_stock_http, "http_get_json", lambda *args, **kwargs: calls.append(args)
    )

    bars = a_stock_http.fetch_tencent_kline("sh600519", "sh", 2)

    assert bars[0]["date"] == (today - timedelta(days=1)).isoformat()
    assert bars[1]["close"] == 10.2
    assert calls == []


def test_daily_kline_falls_back_to_network_when_local_history_is_short(monkeypatch):
    calls = []
    monkeypatch.setattr(
        local_market_history,
        "get_daily_bars",
        lambda codes, end_date, lookback: [_local_bar("2026-08-18")],
    )
    monkeypatch.setattr(
        a_stock_http,
        "http_get_json",
        lambda url, **kwargs: calls.append(url) or _tencent_response(),
    )

    bars = a_stock_http.fetch_tencent_kline("600519", "sh", 2)

    assert len(bars) == 2
    assert calls and "sh600519,day" in calls[0]


def test_daily_kline_does_not_use_future_local_bar(monkeypatch):
    calls = []
    today = date.today()
    monkeypatch.setattr(
        local_market_history,
        "get_daily_bars",
        lambda codes, end_date, lookback: [
            _local_bar((today + timedelta(days=1)).isoformat()),
            _local_bar((today + timedelta(days=2)).isoformat()),
        ],
    )
    monkeypatch.setattr(
        a_stock_http,
        "http_get_json",
        lambda url, **kwargs: calls.append(url) or _tencent_response(),
    )

    a_stock_http.fetch_tencent_kline("600519", "sh", 2)

    assert calls


def test_intraday_klines_still_use_network(monkeypatch):
    calls = []
    monkeypatch.setattr(
        local_market_history,
        "get_daily_bars",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("local")),
    )
    monkeypatch.setattr(
        a_stock_http,
        "http_get_json",
        lambda url, **kwargs: calls.append(url) or _tencent_response(
            "qfq60" if ",60" in url else "qfq30"
        ),
    )

    assert a_stock_http.fetch_tencent_kline("600519", "sh", 2, "60")
    assert a_stock_http.fetch_tencent_kline("600519", "sh", 2, "30")
    assert len(calls) == 2
    assert ",60" in calls[0]
    assert ",30" in calls[1]


def test_market_adapter_tencent_wrapper_remains_transparent(monkeypatch):
    expected = [{"date": "2026-08-18", "close": 10.0}]
    received = {}

    def fake_fetch(code, market, days, ktype):
        received.update(code=code, market=market, days=days, ktype=ktype)
        return expected

    monkeypatch.setattr(market_adapters, "_fetch_tencent_kline", fake_fetch)

    assert market_adapters.fetch_tencent_kline(
        "600519", market="sh", days=2, ktype="day"
    ) == expected
    assert received == {"code": "600519", "market": "sh", "days": 2, "ktype": "day"}


def test_daily_kline_skips_local_bar_with_null_volume(monkeypatch):
    """缓存里单行 volume 为 NULL 时剔除该行，其余照常返回，不崩、不联网。"""
    calls = []
    today = date.today()
    monkeypatch.setattr(
        local_market_history,
        "get_daily_bars",
        lambda codes, end_date, lookback: [
            _local_bar((today - timedelta(days=2)).isoformat()),
            {**_local_bar((today - timedelta(days=1)).isoformat()), "volume": None},
            _local_bar(today.isoformat(), 10.2),
        ],
    )
    monkeypatch.setattr(
        a_stock_http, "http_get_json", lambda *args, **kwargs: calls.append(args)
    )

    bars = a_stock_http.fetch_tencent_kline("sh600519", "sh", 2)

    assert [bar["date"] for bar in bars] == [
        (today - timedelta(days=2)).isoformat(),
        today.isoformat(),
    ]
    assert calls == []


def test_daily_kline_falls_back_to_network_when_null_field_shortens_history(monkeypatch):
    """剔除坏行后本地不足 days 时，回退网络取数。"""
    calls = []
    monkeypatch.setattr(
        local_market_history,
        "get_daily_bars",
        lambda codes, end_date, lookback: [
            _local_bar("2026-08-18"),
            {**_local_bar("2026-08-19"), "close": None},
        ],
    )
    monkeypatch.setattr(
        a_stock_http,
        "http_get_json",
        lambda url, **kwargs: calls.append(url) or _tencent_response(),
    )

    bars = a_stock_http.fetch_tencent_kline("600519", "sh", 2)

    assert len(bars) == 2
    assert calls and "sh600519,day" in calls[0]


def test_network_kline_skips_malformed_row(monkeypatch):
    """网络路径单行字段缺失/非数值时跳过该行，不拖垮整个取数。"""
    response = {
        "data": {
            "sh600519": {
                "qfqday": [
                    ["2026-08-17", "10", "10.2", "10.3", "9.9", "100"],
                    ["2026-08-18", "10.2", "10.4", "10.5", "10.1", None],
                    ["2026-08-19", "10.4", "10.6", "10.7", "10.3", "120"],
                ]
            }
        }
    }
    monkeypatch.setattr(
        local_market_history,
        "get_daily_bars",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        a_stock_http, "http_get_json", lambda url, **kwargs: response
    )

    bars = a_stock_http.fetch_tencent_kline("600519", "sh", 3)

    assert [bar["date"] for bar in bars] == ["2026-08-17", "2026-08-19"]
