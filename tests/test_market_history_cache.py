import importlib.util
import json
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("market_history_cache", ROOT / "scripts" / "market_history_cache.py")
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def test_run_fetches_missing_bars_and_upserts(monkeypatch):
    calls = []
    monkeypatch.setattr(module.history, "ensure_schema", lambda: None)
    monkeypatch.setattr(module.history, "cache_stats", lambda: {"row_count": 1})
    monkeypatch.setattr(module.history, "get_latest_daily_bars", lambda codes, asof: [])
    monkeypatch.setattr(module, "BaoStockSession", lambda: _NoopSession())
    monkeypatch.setattr(module.history, "upsert_daily_bars", lambda rows: calls.append(rows) or len(rows))
    monkeypatch.setattr(module, "fetch_baostock", lambda code, start_date, end_date, *, session: [{
        "code": code, "trading_date": end_date, "close": 10.0,
    }])

    result = module.run(asof="2026-08-18", codes=["600000", "000001"])

    assert result["status"] == "ok"
    assert result["fetched"] == result["upserted"] == 2
    assert [row[0]["code"] for row in calls] == ["600000", "000001"]


class _NoopSession:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_dry_run_does_not_write(monkeypatch):
    monkeypatch.setattr(module.history, "ensure_schema", lambda: None)
    monkeypatch.setattr(module.history, "cache_stats", lambda: {})
    monkeypatch.setattr(module.history, "get_latest_daily_bars", lambda codes, asof: [])
    monkeypatch.setattr(module, "BaoStockSession", lambda: _NoopSession())
    monkeypatch.setattr(module.history, "upsert_daily_bars", lambda rows: (_ for _ in ()).throw(AssertionError("write")))
    monkeypatch.setattr(module, "fetch_baostock", lambda *args, **kwargs: [{"code": "600000", "trading_date": "2026-08-18"}])

    result = module.run(asof="2026-08-18", codes=["600000"], dry_run=True)

    assert result["fetched"] == 1
    assert result["upserted"] == 0


def test_missing_baostock_is_a_non_fatal_blocked_result(monkeypatch):
    monkeypatch.setattr(module.history, "ensure_schema", lambda: None)
    monkeypatch.setattr(module.history, "cache_stats", lambda: {})
    monkeypatch.setattr(module.history, "get_latest_daily_bars", lambda codes, asof: [])
    monkeypatch.setattr(module, "BaoStockSession", lambda: _MissingSession())

    result = module.run(asof="2026-08-18", codes=["600000"])

    assert result["status"] == "blocked"
    assert result["reason"] == "baostock_not_installed"
    assert result["failed"] == [{"code": "*", "reason": "baostock_not_installed"}]


class _MissingSession:
    def __enter__(self):
        raise RuntimeError("baostock_not_installed")

    def __exit__(self, *args):
        return None


def test_empty_cache_defaults_to_full_backfill_start_and_one_session(monkeypatch):
    calls = {"login": 0, "logout": 0, "starts": []}

    class Session:
        def __enter__(self):
            calls["login"] += 1
            return self

        def __exit__(self, *args):
            calls["logout"] += 1

    monkeypatch.setattr(module.history, "ensure_schema", lambda: None)
    monkeypatch.setattr(module.history, "cache_stats", lambda: {"row_count": 0})
    monkeypatch.setattr(module.history, "get_latest_daily_bars", lambda codes, asof: [])
    monkeypatch.setattr(module.history, "upsert_daily_bars", lambda rows: len(rows))
    monkeypatch.setattr(module, "BaoStockSession", lambda: Session())

    def fetch(code, start_date, end_date, *, session):
        calls["starts"].append(start_date)
        return [{"code": code, "trading_date": end_date}]

    monkeypatch.setattr(module, "fetch_baostock", fetch)
    result = module.run(asof="2026-08-18", codes=["600000", "000001"])

    assert result["full_backfill"] is True
    assert calls["starts"] == ["1990-01-01", "1990-01-01"]
    assert calls["login"] == calls["logout"] == 1


def test_universe_loader_prefers_exchange_universe(tmp_path, monkeypatch):
    data = tmp_path / "skills" / "stock-triage" / "data"
    data.mkdir(parents=True)
    (data / "exchange_universe.json").write_text(json.dumps({"stocks": [{"code": "sh.600000"}, {"code": "000001"}]}))
    (data / "universe_quotes_cache.json").write_text(json.dumps({"stocks": [{"code": "600002"}]}))
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    assert module.load_universe() == ["600000", "000001"]


def test_fetch_baostock_uses_mocked_provider_without_network(monkeypatch):
    class Login:
        error_code = "0"
        error_msg = ""

    class Result:
        def __init__(self):
            self.rows = [["2026-08-18", "9", "11", "8", "10", "9.5", "100", "1000", "1.2", "5"]]

        def next(self):
            return bool(self.rows)

        def get_row_data(self):
            return self.rows.pop(0)

    fake = types.SimpleNamespace(
        login=lambda: Login(),
        query_history_k_data_plus=lambda *args, **kwargs: Result(),
        logout=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "baostock", fake)

    rows = module.fetch_baostock("600000", "2026-08-18", "2026-08-18")

    assert rows[0]["code"] == "600000"
    assert rows[0]["close"] == 10.0
    assert rows[0]["source"] == "baostock"
