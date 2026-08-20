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
    # Fetch order is staleness-first (see fetch_order); with both symbols
    # uncached the tiebreak is the code itself, so it stays deterministic.
    assert [row[0]["code"] for row in calls] == ["000001", "600000"]


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


def test_empty_cache_seeds_a_bounded_window_in_one_session(monkeypatch):
    """An empty cache must not trigger a fetch from 1990.

    Measured 2026-08-20 against BaoStock: ~17s/symbol from
    FULL_BACKFILL_START_DATE, i.e. ~24h for the 5206-symbol universe — a run
    that could never finish inside any cron budget, and the reason every
    market-history-cache run was SIGKILLed. The bounded window is ~0.1s/symbol.
    """
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

    assert result["full_backfill"] is False
    assert calls["starts"] == ["2025-12-21", "2025-12-21"]  # asof - 240 days
    assert calls["login"] == calls["logout"] == 1


def test_full_backfill_flag_still_reaches_the_full_history(monkeypatch):
    """The 1990 fetch stays available — as a deliberate request, not a default."""
    starts = []
    monkeypatch.setattr(module.history, "ensure_schema", lambda: None)
    monkeypatch.setattr(module.history, "cache_stats", lambda: {"row_count": 0})
    monkeypatch.setattr(module.history, "get_latest_daily_bars", lambda codes, asof: [])
    monkeypatch.setattr(module.history, "upsert_daily_bars", lambda rows: len(rows))
    monkeypatch.setattr(module, "BaoStockSession", lambda: _NoopSession())
    monkeypatch.setattr(
        module, "fetch_baostock",
        lambda code, start_date, end_date, *, session: starts.append(start_date) or [],
    )

    result = module.run(asof="2026-08-18", codes=["600000"], full_backfill=True)

    assert result["full_backfill"] is True
    assert starts == [module.FULL_BACKFILL_START_DATE]


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


# --------------------------------------------------------------------------
# Budget, ordering and per-symbol backfill.
#
# Measured 2026-08-20 against real BaoStock: a full incremental pass over the
# 5206-symbol universe costs ~520-2500s, so the 300s cron budget could never
# be met and every run ended in SIGKILL. These tests pin the three behaviours
# that turn "killed with no report" into "bounded, honest, and converging".
# --------------------------------------------------------------------------

import time as _time


def _stub_run(monkeypatch, *, latest_rows, fetch, row_count=500):
    monkeypatch.setattr(module.history, "ensure_schema", lambda: None)
    monkeypatch.setattr(module.history, "cache_stats", lambda: {"row_count": row_count})
    monkeypatch.setattr(module.history, "get_latest_daily_bars", lambda c, a: latest_rows)
    monkeypatch.setattr(module.history, "upsert_daily_bars", lambda rows: len(rows))
    monkeypatch.setattr(module, "BaoStockSession", lambda: _NoopSession())
    monkeypatch.setattr(module, "fetch_baostock", fetch)


class TestPerSymbolBackfill:
    def test_an_uncached_symbol_is_backfilled_even_when_others_are_current(self, monkeypatch):
        """The gap that never closed and never surfaced.

        full_backfill used to be decided once per run from row_count, so the
        moment ONE symbol had rows, every never-fetched symbol started at
        `asof` and could only ever hold a single bar.
        """
        starts = {}

        def fetch(code, start_date, end_date, *, session):
            starts[code] = start_date
            return [{"code": code, "trading_date": end_date}]

        _stub_run(
            monkeypatch,
            latest_rows=[{"code": "600000", "trading_date": "2026-08-18"}],
            fetch=fetch,
        )

        module.run(asof="2026-08-19", codes=["600000", "000001"])

        assert starts["600000"] == "2026-08-19"   # cached: resume from latest+1
        assert starts["000001"] == "2025-12-22"   # uncached: asof - 240 days

    def test_start_date_is_a_pure_function_of_that_symbols_own_state(self):
        assert module._start_date(None, "2026-08-19") == "2025-12-22"
        assert module._start_date("2026-08-18", "2026-08-19") == "2026-08-19"
        assert module._start_date(None, "2026-08-19", full_backfill=True) == "1990-01-01"


class TestFetchOrder:
    def test_never_cached_first_then_stalest_then_code(self):
        order = module.fetch_order(
            ["600000", "000001", "300750", "002594"],
            {"600000": "2026-08-18", "300750": "2026-07-01"},
        )

        assert order == ["000001", "002594", "300750", "600000"]

    def test_a_budget_bound_run_advances_instead_of_redoing_the_prefix(self, monkeypatch):
        """Natural universe order meant the tail was never fetched at all."""
        universe = [f"{i:06d}" for i in range(6)]
        # Day 1 left the last three symbols untouched.
        latest = {code: "2026-08-19" for code in universe[:3]}
        seen = []

        def fetch(code, start_date, end_date, *, session):
            seen.append(code)
            return []

        _stub_run(
            monkeypatch,
            latest_rows=[{"code": c, "trading_date": d} for c, d in latest.items()],
            fetch=fetch,
        )
        module.run(asof="2026-08-20", codes=universe)

        assert seen[:3] == universe[3:], "the untouched tail must be served first"


class TestBudget:
    def test_the_run_stops_inside_the_job_budget_and_says_what_is_left(self, monkeypatch):
        monkeypatch.setenv("A_STOCK_JOB_TIMEOUT_SECONDS", "1")

        def fetch(code, start_date, end_date, *, session):
            _time.sleep(0.05)
            return [{"code": code, "trading_date": end_date}]

        _stub_run(monkeypatch, latest_rows=[], fetch=fetch)

        started = _time.monotonic()
        result = module.run(asof="2026-08-19", codes=[f"{i:06d}" for i in range(200)])
        elapsed = _time.monotonic() - started

        assert elapsed < 2.0, f"ran {elapsed:.2f}s against a 1s job budget"
        assert result["budget_exhausted"] is True
        assert result["status"] == "partial"
        assert "budget_exhausted" in result["reason"]
        assert result["processed"] + result["remaining"] == 200
        assert result["remaining"] > 0

    def test_rows_already_fetched_are_kept_when_the_budget_runs_out(self, monkeypatch):
        """Being SIGKILLed threw away everything learned. Stopping must not."""
        monkeypatch.setenv("A_STOCK_JOB_TIMEOUT_SECONDS", "1")

        def fetch(code, start_date, end_date, *, session):
            _time.sleep(0.05)
            return [{"code": code, "trading_date": end_date}]

        _stub_run(monkeypatch, latest_rows=[], fetch=fetch)

        result = module.run(asof="2026-08-19", codes=[f"{i:06d}" for i in range(200)])

        assert result["upserted"] == result["processed"] > 0

    def test_a_manual_run_has_no_budget(self, monkeypatch):
        monkeypatch.delenv("A_STOCK_JOB_TIMEOUT_SECONDS", raising=False)
        _stub_run(
            monkeypatch, latest_rows=[],
            fetch=lambda code, s, e, *, session: [{"code": code, "trading_date": e}],
        )

        result = module.run(asof="2026-08-19", codes=["600000", "000001"])

        assert result["budget_seconds"] is None
        assert result["budget_exhausted"] is False
        assert result["remaining"] == 0
        assert result["status"] == "ok"

    def test_budget_is_derived_from_the_job_timeout_not_invented(self, monkeypatch):
        monkeypatch.setenv("A_STOCK_JOB_TIMEOUT_SECONDS", "300")
        assert module.fetch_budget_seconds() == 300 * module.FETCH_BUDGET_RATIO
        monkeypatch.setenv("A_STOCK_JOB_TIMEOUT_SECONDS", "not-a-number")
        assert module.fetch_budget_seconds() is None
        monkeypatch.setenv("A_STOCK_JOB_TIMEOUT_SECONDS", "0")
        assert module.fetch_budget_seconds() is None

    def test_a_failing_symbol_still_counts_as_processed(self, monkeypatch):
        """Otherwise a permanently broken symbol would be retried forever."""
        def fetch(code, start_date, end_date, *, session):
            raise ValueError("boom")

        _stub_run(monkeypatch, latest_rows=[], fetch=fetch)

        result = module.run(asof="2026-08-19", codes=["600000", "000001"])

        assert result["processed"] == 2
        assert result["remaining"] == 0
        assert result["status"] == "partial"
        assert len(result["failed"]) == 2


def test_stdout_stays_parseable_json_even_though_baostock_chatters(monkeypatch, capsys):
    """The runner does json.loads() on the whole stdout stream.

    BaoStock's "login success!" went to stdout, so the parse always failed and
    this job never produced a market snapshot — silently, because a failed
    parse is not an error.
    """
    class Login:
        error_code = "0"
        error_msg = ""

    def _login():
        print("login success!")
        return Login()

    def _logout():
        print("logout success!")

    class Result:
        def next(self):
            return False

        def get_row_data(self):
            return []

    fake = types.SimpleNamespace(
        login=_login,
        query_history_k_data_plus=lambda *a, **k: Result(),
        logout=_logout,
    )
    monkeypatch.setitem(sys.modules, "baostock", fake)
    monkeypatch.setattr(module.history, "ensure_schema", lambda: None)
    monkeypatch.setattr(module.history, "cache_stats", lambda: {"row_count": 1})
    monkeypatch.setattr(module.history, "get_latest_daily_bars", lambda c, a: [])
    monkeypatch.setattr(module.history, "upsert_daily_bars", lambda rows: len(rows))

    assert module.main(["--json", "--asof", "2026-08-19", "--codes", "600000"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # the whole stream, exactly as the runner reads it
    assert payload["status"] == "ok"
    assert "login success!" in captured.err


def test_a_failed_login_restores_stdout_instead_of_leaking_the_redirect(monkeypatch):
    """The redirect is handed to the session only after login succeeds."""
    class Login:
        error_code = "10001"
        error_msg = "network unreachable"

    fake = types.SimpleNamespace(
        login=lambda: Login(), query_history_k_data_plus=None, logout=lambda: None
    )
    monkeypatch.setitem(sys.modules, "baostock", fake)
    before = sys.stdout

    try:
        with module.BaoStockSession():
            raise AssertionError("unreachable")
    except RuntimeError as exc:
        assert "baostock_login_failed" in str(exc)

    assert sys.stdout is before, "stdout stayed redirected after a failed login"
