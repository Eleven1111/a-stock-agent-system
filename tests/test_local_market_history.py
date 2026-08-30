import sqlite3

import local_market_history as history


def _bar(code="600000", trading_date="2026-08-18", close=10.0, adjust_flag="qfq"):
    return {
        "code": code,
        "trading_date": trading_date,
        "adjust_flag": adjust_flag,
        "open": close - 0.5,
        "high": close + 0.5,
        "low": close - 1,
        "close": close,
        "preclose": close - 0.2,
        "volume": 1000,
        "amount": 10000,
        "turn": 1.2,
        "pct_chg": 2.0,
        "source": "fixture",
        "source_version": "v1",
    }


def test_ensure_schema_creates_required_table_and_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    history.ensure_schema()

    database = tmp_path / "market" / "history.sqlite3"
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(daily_bars)")}
    assert {"code", "trading_date", "adjust_flag", "close", "updated_at"} <= columns


def test_upsert_is_idempotent_and_updates_existing_bar(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    row = _bar()

    assert history.upsert_daily_bars([row]) == 1
    row["close"] = 11.0
    assert history.upsert_daily_bars([row]) == 1

    assert history.cache_stats()["row_count"] == 1
    assert history.get_latest_daily_bars(["600000"])[0]["close"] == 11.0


def test_partial_fallback_does_not_erase_richer_existing_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    original = _bar()
    history.upsert_daily_bars([original])

    history.upsert_daily_bars([{
        "code": "600000", "trading_date": "2026-08-18",
        "open": 10.5, "high": 12.0, "low": 10.0, "close": 11.0,
        "volume": 45600.0, "amount": None, "turn": None,
        "source": "tencent_qfqday",
        "source_version": "ifzq-fqkline-qfq-v1",
    }])

    row = history.get_latest_daily_bars(["600000"])[0]
    assert (row["open"], row["high"], row["low"], row["close"], row["volume"]) == (
        10.5, 12.0, 10.0, 11.0, 45600.0,
    )
    assert row["amount"] == original["amount"]
    assert row["turn"] == original["turn"]
    assert row["source"] == "tencent_qfqday"
    assert row["source_version"] == "ifzq-fqkline-qfq-v1"


def test_lookback_returns_recent_rows_per_code_in_sorted_order(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    history.upsert_daily_bars([
        _bar("600000", "2026-08-18"), _bar("600000", "2026-08-17"),
        _bar("600000", "2026-08-16"), _bar("000001", "2026-08-17"),
    ])

    result = history.get_daily_bars(["600000", "000001"], "2026-08-18", 2)

    assert [(row["code"], row["trading_date"]) for row in result] == [
        ("000001", "2026-08-17"), ("600000", "2026-08-17"), ("600000", "2026-08-18")
    ]


def test_latest_returns_one_bar_per_code_as_of_date(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    history.upsert_daily_bars([_bar("600000", "2026-08-17"), _bar("600000", "2026-08-18")])

    result = history.get_latest_daily_bars(["600000"], "2026-08-17")

    assert result[0]["trading_date"] == "2026-08-17"


def test_state_home_isolates_databases(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(first))
    history.upsert_daily_bars([_bar()])
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(second))

    assert history.cache_stats()["row_count"] == 0
    assert (second / "market" / "history.sqlite3").exists()


def test_coverage_by_code_includes_missing_and_shallow_symbols(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    history.upsert_daily_bars([
        _bar("600000", "2026-08-17"),
        _bar("600000", "2026-08-18"),
        _bar("000001", "2026-08-18"),
    ])

    coverage = history.coverage_by_code(
        ["600000", "000001", "300750"], "2026-08-18"
    )

    assert coverage["600000"] == {
        "code": "600000", "bar_count": 2,
        "min_date": "2026-08-17", "max_date": "2026-08-18",
    }
    assert coverage["000001"]["bar_count"] == 1
    assert coverage["300750"] == {
        "code": "300750", "bar_count": 0,
        "min_date": None, "max_date": None,
    }
