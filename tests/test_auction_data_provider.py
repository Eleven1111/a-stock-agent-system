"""Contract tests for the real auction/previous-volume provider boundary."""

from datetime import date
import json
from pathlib import Path
import struct

import auction_data_provider as provider


def test_easy_tdx_rows_are_normalized_from_shares_to_lots_and_windowed():
    rows = provider.normalize_auction_rows([
        {"time": "09:26:00", "price": 10.9, "matched": 999, "unmatched": 1},
        {"time": "09:25:00", "price": 11.0, "matched": 3500, "unmatched": 0},
        {"time": "09:15:00", "price": 10.5, "matched": 50, "unmatched": 2500},
    ])

    assert [row["t"] for row in rows] == ["09:15:00", "09:25:00"]
    assert rows[-1]["matched"] == 3500
    assert rows[-1]["unmatched"] == 0
    assert rows[-1]["volume"] == 35.0
    assert rows[-1]["matched_unit"] == "share"
    assert rows[-1]["volume_unit"] == "lot"
    assert rows[-1]["provider"] == "easy_tdx_mac_0x123d"


def test_easy_tdx_0x123d_response_parser_exposes_share_units():
    from easy_tdx.mac.commands.symbol_auction import SymbolAuctionCmd

    header = struct.pack("<H22sI", 1, b"600519", 1) + bytes(8)
    item = struct.pack("<IfIi", 9 * 3600 + 25 * 60, 1510.0, 3500, 0)
    parsed = SymbolAuctionCmd(1, "600519").parse_response(header + item)

    assert parsed[0].time.strftime("%H:%M:%S") == "09:25:00"
    assert parsed[0].matched == 3500
    assert parsed[0].unmatched == 0


def test_real_fixture_replay_preserves_easy_tdx_contract():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "auction" / "easy_tdx_20260817_600519.json")
        .read_text(encoding="utf-8")
    )
    rows = provider.normalize_auction_rows(fixture["rows"])

    assert [row["t"] for row in rows] == ["09:15:00", "09:20:00", "09:24:00", "09:25:00"]
    assert rows[-1]["matched"] == 3500
    assert rows[-1]["volume"] == 35.0
    assert rows[-1]["unmatched"] == 0


def test_previous_day_volume_uses_mootdx_bar_before_event_day(monkeypatch):
    monkeypatch.setattr(provider.local_market_history, "get_latest_daily_bars", lambda *args: [])
    monkeypatch.setattr(provider, "fetch_mootdx_bars", lambda code, days=10: [
        {"date": "2026-08-17", "volume": 999999, "amount": 9},
        {"date": "2026-08-14", "volume": 123456, "amount": 4567890},
    ])

    result = provider.fetch_previous_day_metrics("600519", asof=date(2026, 8, 17))

    assert result == {
        "prev_day_volume": 123456.0,
        "prev_day_amount": 4567890.0,
        "prev_day_date": "2026-08-14",
        "prev_day_provider": "mootdx",
        "prev_day_provenance": {
            "provider": "mootdx",
            "dataset": "daily_bars",
            "date": "2026-08-14",
        },
    }


def test_previous_day_volume_falls_back_to_tencent_kline_and_filters_asof(monkeypatch):
    monkeypatch.setattr(provider.local_market_history, "get_latest_daily_bars", lambda *args: [])
    monkeypatch.setattr(provider, "fetch_mootdx_bars", lambda code, days=10: [])
    monkeypatch.setattr(provider, "fetch_tencent_kline", lambda code, market, days, ktype: [
        {"date": "2026-08-18", "volume": 888},
        {"date": "2026-08-17", "volume": 777},
        {"date": "2026-08-15", "volume": 0},
        {"date": "2026-08-14", "volume": 222, "amount": 3333},
        {"date": "2026-08-13", "volume": 111},
    ])

    result = provider.fetch_previous_day_metrics("600519", asof=date(2026, 8, 17))

    assert result["prev_day_volume"] == 222.0
    assert result["prev_day_date"] == "2026-08-14"
    assert result["prev_day_provider"] == "tencent_kline"
    assert result["prev_day_provenance"] == {
        "provider": "tencent_kline",
        "dataset": "daily_kline",
        "date": "2026-08-14",
    }


def test_previous_day_volume_falls_back_when_mootdx_has_no_eligible_bar(monkeypatch):
    monkeypatch.setattr(provider.local_market_history, "get_latest_daily_bars", lambda *args: [])
    monkeypatch.setattr(provider, "fetch_mootdx_bars", lambda code, days=10: [
        {"date": "2026-08-17", "volume": 999},
    ])
    monkeypatch.setattr(provider, "fetch_tencent_kline", lambda code, market, days, ktype: [
        {"date": "2026-08-14", "volume": 123},
    ])

    result = provider.fetch_previous_day_metrics("000001", asof="2026-08-17")

    assert result["prev_day_provider"] == "tencent_kline"
    assert result["prev_day_volume"] == 123.0


def test_previous_day_volume_all_sources_fail_closed(monkeypatch):
    monkeypatch.setattr(provider.local_market_history, "get_latest_daily_bars", lambda *args: [])
    monkeypatch.setattr(provider, "fetch_mootdx_bars", lambda code, days=10: [])
    monkeypatch.setattr(provider, "fetch_tencent_kline", lambda code, market, days, ktype: [])

    assert provider.fetch_previous_day_metrics("600519", asof="2026-08-17") == {}


def test_previous_day_volume_uses_local_history_before_network(monkeypatch):
    calls = []

    monkeypatch.setattr(provider.local_market_history, "get_latest_daily_bars", lambda codes, end_date: [{
        "code": "600519", "trading_date": "2026-08-14", "volume": 123456,
        "amount": 4567890, "source_version": "history-fixture-v2",
    }])
    monkeypatch.setattr(provider, "fetch_mootdx_bars", lambda *args, **kwargs: calls.append("mootdx"))
    monkeypatch.setattr(provider, "fetch_tencent_kline", lambda *args, **kwargs: calls.append("tencent"))

    result = provider.fetch_previous_day_metrics("sh600519", asof="2026-08-17")

    assert result["prev_day_provider"] == "local_history"
    assert result["prev_day_source_version"] == "history-fixture-v2"
    assert result["prev_day_provenance"] == {
        "provider": "local_history", "dataset": "daily_bars",
        "date": "2026-08-14", "source_version": "history-fixture-v2",
    }
    assert calls == []


def test_batch_uses_supplied_previous_day_metrics_without_historical_fetch(monkeypatch):
    import sys
    import types

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_auction(self, market, code):
            return [{"time": "09:15:00", "price": 10, "matched": 100, "unmatched": 50}]

    class FakeMacClient:
        @staticmethod
        def from_best_host():
            return FakeClient()

    fake_module = types.ModuleType("easy_tdx")
    fake_module.MacClient = FakeMacClient
    monkeypatch.setitem(sys.modules, "easy_tdx", fake_module)
    monkeypatch.setattr(
        provider,
        "fetch_previous_day_metrics",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("must not fetch history")
        ),
    )

    snapshots, failures = provider.fetch_real_auction_snapshots(
        ["sh600519"],
        asof="2026-08-18",
        previous_day_metrics={
            "600519": {
                "prev_day_volume": 1000, "prev_day_amount": 20000, "prev_close": 10.5
            }
        },
    )

    assert not failures
    assert snapshots["600519"][0]["prev_day_volume"] == 1000


def test_easy_tdx_daily_kline_provides_previous_day_metrics_and_is_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    class FakeFrame:
        def to_dict(self, orient):
            assert orient == "records"
            return [
                {"datetime": "2026-08-19", "vol": 8888, "amount": 9999, "close": 11.0},
                {"datetime": "2026-08-18", "vol": 7777, "amount": 8888, "close": 10.5},
            ]

    class FakeClient:
        def get_stock_kline(self, market, code, period, start, count):
            assert (market, code, start, count) == (1, "600519", 0, 10)
            return FakeFrame()

    result = provider.fetch_easy_tdx_previous_day_metrics(
        FakeClient(), "600519", asof="2026-08-19"
    )

    assert result["prev_day_volume"] == 7777.0
    assert result["prev_day_amount"] == 8888.0
    assert result["prev_day_date"] == "2026-08-18"
    assert result["prev_day_provider"] == "easy_tdx_daily"
    assert result["prev_close"] == 10.5
    assert provider.local_market_history.get_latest_daily_bars(
        ["600519"], "2026-08-19"
    )[0]["volume"] == 7777.0


def test_batch_falls_back_to_easy_tdx_daily_kline_when_other_history_sources_fail(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(provider, "fetch_mootdx_bars", lambda *args, **kwargs: [])
    monkeypatch.setattr(provider, "fetch_tencent_kline", lambda *args, **kwargs: [])

    class FakeFrame:
        def to_dict(self, orient):
            return [{"datetime": "2026-08-18", "vol": 7777, "amount": 8888, "close": 10.5}]

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_auction(self, market, code):
            return [{"time": "09:25:00", "price": 10, "matched": 100, "unmatched": 50}]

        def get_stock_kline(self, market, code, period, start, count):
            return FakeFrame()

    class FakeMacClient:
        @staticmethod
        def from_best_host():
            return FakeClient()

    import sys
    import types
    fake_module = types.ModuleType("easy_tdx")
    fake_module.MacClient = FakeMacClient
    fake_module.Period = types.SimpleNamespace(DAILY="daily")
    monkeypatch.setitem(sys.modules, "easy_tdx", fake_module)

    snapshots, failures = provider.fetch_real_auction_snapshots(
        ["sh600519"], asof="2026-08-19"
    )

    assert not failures
    assert snapshots["600519"][0]["prev_day_provider"] == "easy_tdx_daily"
    assert snapshots["600519"][0]["prev_day_volume"] == 7777.0


def test_batch_can_skip_history_for_full_market_intelligence(monkeypatch):
    import sys
    import types

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_auction(self, market, code):
            return [{"time": "09:25:00", "price": 10, "matched": 100, "unmatched": 50}]

    class FakeMacClient:
        @staticmethod
        def from_best_host():
            return FakeClient()

    fake_module = types.ModuleType("easy_tdx")
    fake_module.MacClient = FakeMacClient
    monkeypatch.setitem(sys.modules, "easy_tdx", fake_module)
    monkeypatch.setattr(
        provider,
        "fetch_previous_day_metrics",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("full-market intelligence must not fetch history")
        ),
    )

    snapshots, failures = provider.fetch_real_auction_snapshots(
        ["sh600519"], asof="2026-08-19", require_previous_day_metrics=False
    )

    assert not failures
    assert snapshots["600519"][0]["matched"] == 100
    assert "prev_day_volume" not in snapshots["600519"][0]


def test_mootdx_daily_adapter_requests_count_with_offset(monkeypatch):
    import mootdx_adapter

    class FakeFrame:
        empty = False
        columns = ["datetime", "open", "close", "high", "low", "vol", "amount"]

        def iterrows(self):
            return iter([(0, {
                "datetime": "2026-08-14 00:00:00", "open": 1, "close": 1,
                "high": 1, "low": 1, "vol": 123, "amount": 456,
            })])

    class FakeClient:
        def bars(self, **kwargs):
            assert kwargs["offset"] == 10
            assert "count" not in kwargs
            return FakeFrame()

    monkeypatch.setattr(mootdx_adapter, "_get_client", lambda: FakeClient())
    assert mootdx_adapter.fetch_mootdx_bars("600519", days=10)[0]["volume"] == 123


def test_missing_easy_tdx_or_previous_volume_is_blocked(monkeypatch):
    monkeypatch.setattr(provider, "fetch_easy_tdx_auction", lambda code: [])
    assert provider.fetch_real_auction_observation("600519", asof="2026-08-17")["status"] == "blocked"

    monkeypatch.setattr(provider, "fetch_easy_tdx_auction", lambda code: [
        {"t": "09:25:00", "price": 10.0, "matched": 100, "unmatched": 0,
         "volume": 1.0, "provider": "easy_tdx_mac_0x123d"}
    ])
    monkeypatch.setattr(provider, "fetch_previous_day_metrics", lambda code, asof: {})
    blocked = provider.fetch_real_auction_observation("600519", asof="2026-08-17")
    assert blocked["status"] == "blocked"
    assert "prev_day_volume" in blocked["reason"]
