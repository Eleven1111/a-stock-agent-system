import sys
import types

import minute_rows_source as source


def _result(rows, *, error_code="0", error_msg="success"):
    class Result:
        def __init__(self):
            self.rows = list(rows)
            self.error_code = error_code
            self.error_msg = error_msg

        def next(self):
            return bool(self.rows)

        def get_row_data(self):
            return self.rows.pop(0)

    return Result()


def test_baostock_normalization_uses_close_time_and_share_units(monkeypatch):
    calls = []
    rows = [[
        "2026-08-27", "20260827093500000", "10", "10.1", "9.9", "10.0",
        "123400", "1234567.89",
    ]]

    class Login:
        error_code = "0"
        error_msg = "success"

    fake = types.SimpleNamespace(
        login=lambda: Login(),
        logout=lambda: None,
        query_history_k_data_plus=lambda *args, **kwargs: (
            calls.append((args, kwargs)) or _result(rows)
        ),
    )
    monkeypatch.setitem(sys.modules, "baostock", fake)

    result = source.rows_from_baostock([("2026-08-27", "600519")])

    assert result[("2026-08-27", "600519")] == [{
        "minute": 575,
        "time": "09:35",
        "open": 10.0,
        "high": 10.1,
        "low": 9.9,
        "close": 10.0,
        "volume_shares": 123400.0,
        "amount": 1234567.89,
    }]
    args, kwargs = calls[0]
    assert args[0] == "sh.600519"
    assert kwargs["frequency"] == "5"
    assert kwargs["adjustflag"] == "2"


def test_auto_prefers_store_then_baostock_then_sina(monkeypatch):
    events = [
        {"date": "2026-08-25", "code": "600001"},
        {"date": "2026-08-25", "code": "600002"},
        {"date": "2026-08-25", "code": "600003"},
    ]
    row = [{"minute": 575, "time": "09:35", "volume_shares": 1.0, "amount": 10.0}]
    calls = []
    monkeypatch.setattr(source, "rows_from_store", lambda keys: {
        ("2026-08-25", "600001"): row,
    })

    def bao(keys):
        calls.append(("bao", list(keys)))
        return {("2026-08-25", "600002"): row}, None

    def sina(keys, scale=5, sleep=0.2):
        calls.append(("sina", list(keys)))
        return {("2026-08-25", "600003"): row}

    monkeypatch.setattr(source, "_rows_from_baostock_with_error", bao)
    monkeypatch.setattr(source, "rows_from_sina", sina)

    rows, diagnostics = source.collect(events)

    assert list(rows) == [
        ("2026-08-25", "600001"),
        ("2026-08-25", "600002"),
        ("2026-08-25", "600003"),
    ]
    assert calls == [
        ("bao", [("2026-08-25", "600002"), ("2026-08-25", "600003")]),
        ("sina", [("2026-08-25", "600003")]),
    ]
    assert diagnostics["from_store"] == 1
    assert diagnostics["from_baostock"] == 1
    assert diagnostics["from_sina"] == 1


def test_baostock_failure_is_empty_and_auto_can_fall_back(monkeypatch):
    events = [{"date": "2026-08-25", "code": "600001"}]
    row = [{"minute": 575, "time": "09:35", "volume_shares": 1.0, "amount": 10.0}]
    monkeypatch.setattr(source, "rows_from_store", lambda keys: {})
    monkeypatch.setattr(
        source,
        "_rows_from_baostock_with_error",
        lambda keys: ({}, "baostock_login_failed:offline"),
    )
    monkeypatch.setattr(
        source,
        "rows_from_sina",
        lambda keys, scale=5, sleep=0.2: {("2026-08-25", "600001"): row},
    )

    rows, diagnostics = source.collect(events)

    assert rows[("2026-08-25", "600001")] == row
    assert diagnostics["baostock_error"] == "baostock_login_failed:offline"


def test_baostock_mode_never_uses_daily_proxy_or_sina(monkeypatch):
    events = [{"date": "2024-01-03", "code": "600001"}]
    monkeypatch.setattr(
        source,
        "_rows_from_baostock_with_error",
        lambda keys: ({}, "baostock_query_failed"),
    )
    monkeypatch.setattr(
        source,
        "rows_from_sina",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sina fallback")),
    )

    rows, diagnostics = source.collect(events, mode=source.MODE_BAOSTOCK)

    assert rows == {}
    assert diagnostics["covered_keys"] == 0
    assert diagnostics["baostock_error"] == "baostock_query_failed"
