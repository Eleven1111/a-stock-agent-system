import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("market_history_cache", ROOT / "scripts" / "market_history_cache.py")
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


@pytest.fixture(autouse=True)
def _covered_history_by_default(monkeypatch):
    """Legacy orchestration tests are about incrementality, not coverage.

    New coverage-specific tests override this with deliberately shallow rows.
    """
    monkeypatch.setattr(
        module.history,
        "coverage_by_code",
        lambda codes, end_date, **kwargs: {
            code: {
                "code": code,
                "bar_count": 180,
                "min_date": "2025-12-01",
                "max_date": end_date,
            }
            for code in codes
        },
    )


def test_non_trading_day_skips_without_touching_cache_or_provider(monkeypatch):
    monkeypatch.setattr(
        module.history,
        "ensure_schema",
        lambda: (_ for _ in ()).throw(AssertionError("cache touched")),
    )
    monkeypatch.setattr(
        module,
        "BaoStockSession",
        lambda: (_ for _ in ()).throw(AssertionError("provider touched")),
    )

    result = module.run(asof="2026-08-22", codes=["600000"])

    assert result == {
        "status": "skipped",
        "asof": "2026-08-22",
        "reason": "non_trading_day",
        "fetched": 0,
        "upserted": 0,
        "failed": [],
        "cache_stats": {},
        "source_health": module._new_source_health(),
    }


def test_unknown_trading_calendar_is_blocked_not_mislabeled_as_a_holiday(monkeypatch):
    def unavailable(_day):
        raise module.a_share_rules.CalendarCoverageError("calendar does not cover 2027")

    monkeypatch.setattr(module.a_share_rules, "is_trading_day", unavailable)
    monkeypatch.setattr(
        module.history,
        "ensure_schema",
        lambda: (_ for _ in ()).throw(AssertionError("cache touched")),
    )

    result = module.run(asof="2027-01-04", codes=["600000"])

    assert result["status"] == "blocked"
    assert result["reason"] == "trading_calendar_unavailable:calendar does not cover 2027"
    assert result["reason"] != "non_trading_day"
    assert result["fetched"] == result["upserted"] == 0


def test_complete_target_date_cache_skips_without_opening_provider(monkeypatch):
    selected = ["600000", "000001", "000300"]
    monkeypatch.setattr(module.a_share_rules, "is_trading_day", lambda _day: True)
    monkeypatch.setattr(module, "load_universe", lambda: selected)
    monkeypatch.setattr(module.history, "ensure_schema", lambda: None)
    monkeypatch.setattr(module.history, "cache_stats", lambda: {"row_count": 3})
    monkeypatch.setattr(
        module.history,
        "get_latest_daily_bars",
        lambda codes, asof: [
            {"code": code, "trading_date": asof} for code in codes
        ],
    )
    monkeypatch.setattr(
        module,
        "BaoStockSession",
        lambda: (_ for _ in ()).throw(AssertionError("provider touched")),
    )

    result = module.run(asof="2026-08-18")

    assert result["status"] == "skipped"
    assert result["reason"] == "target_date_already_cached"
    assert result["fetched"] == result["upserted"] == 0
    assert result["processed"] == result["remaining"] == 0


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
    assert result["source_health"]["status"] == "healthy"
    assert result["source_health"]["fallback_used"] is False
    assert result["source_health"]["single_source"] is True
    assert result["source_health"]["contributions"] == [{
        "provider": "baostock", "row_count": 2, "stock_count": 2,
        "row_ratio": 1.0,
    }]
    assert result["source_health"]["cross_source_consistency"] == {
        "status": "unavailable", "sample_size": 0,
        "reason": "secondary_provider_not_sampled",
    }
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
    monkeypatch.setattr(module, "EasyTdxSession", lambda: _MissingSession())
    monkeypatch.setattr(module, "TencentSession", lambda: _MissingSession())

    result = module.run(asof="2026-08-18", codes=["600000"])

    assert result["status"] == "blocked"
    assert result["reason"] == "baostock_not_installed"
    assert result["failed"] == [{"code": "*", "reason": "baostock_not_installed"}]


def test_tencent_qfq_fallback_normalizes_volume_and_preserves_contract(monkeypatch):
    monkeypatch.setattr(module, "http_get_json", lambda *args, **kwargs: {
        "data": {"sz000001": {"qfqday": [
            ["2026-08-17", "10", "11", "12", "9", "123"],
            ["2026-08-18", "11", "12", "13", "10", "456"],
        ]}}
    })

    rows = module.TencentSession().fetch("000001", "2026-08-17", "2026-08-18")

    assert rows[1]["volume"] == 45600.0
    assert rows[1]["preclose"] == 11.0
    assert rows[1]["pct_chg"] == pytest.approx(100 / 11)
    assert rows[1]["adjust_flag"] == "qfq"
    assert rows[1]["source"] == "tencent_qfqday"
    assert rows[1]["amount"] is None


def test_easy_tdx_maps_hs300_to_shanghai_market(monkeypatch):
    calls = []

    class Frame:
        def to_dict(self, orient):
            return []

    session = module.EasyTdxSession()
    session.client = types.SimpleNamespace(
        get_stock_kline=lambda market, code, *args, **kwargs: calls.append(
            (market, code, kwargs.get("adjust"))
        ) or Frame()
    )
    fake = types.SimpleNamespace(Adjust=types.SimpleNamespace(QFQ=1), Period=types.SimpleNamespace(DAILY=4))
    monkeypatch.setitem(sys.modules, "easy_tdx", fake)

    session.fetch("000300", "2025-01-01", "2026-08-28")

    assert calls == [(1, "000300", 1)]


class _MissingSession:
    def __enter__(self):
        raise RuntimeError("baostock_not_installed")

    def __exit__(self, *args):
        return None


def test_successful_fallback_is_explicitly_degraded_not_primary_healthy(monkeypatch):
    class PrimaryDown:
        def __enter__(self):
            raise RuntimeError("baostock_login_failed:timeout")

        def __exit__(self, *args):
            return None

    class Tdx:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def fetch(self, code, start_date, end_date):
            return [{
                "code": code, "trading_date": end_date,
                "source": "easy_tdx_qfq", "close": 10.0,
            }]

    monkeypatch.setattr(module.history, "ensure_schema", lambda: None)
    monkeypatch.setattr(module.history, "cache_stats", lambda: {"row_count": 0})
    monkeypatch.setattr(module.history, "get_latest_daily_bars", lambda codes, asof: [])
    monkeypatch.setattr(module.history, "upsert_daily_bars", lambda rows: len(rows))
    monkeypatch.setattr(module, "BaoStockSession", PrimaryDown)
    monkeypatch.setattr(module, "EasyTdxSession", Tdx)

    result = module.run(asof="2026-08-18", codes=["600000", "000001"])

    health = result["source_health"]
    assert result["status"] == "ok"
    assert result["provider"] == "easy_tdx_qfq"
    assert result["provider_fallback_reason"] == "baostock_login_failed:timeout"
    assert health["status"] == "degraded"
    assert health["degraded"] is True
    assert health["fallback_used"] is True
    assert health["active_provider"] == "easy_tdx_qfq"
    assert health["providers"][0] == {
        "provider": "baostock", "role": "primary", "status": "failed",
        "reason": "baostock_login_failed:timeout",
    }
    assert health["contributions"] == [{
        "provider": "easy_tdx_qfq", "row_count": 2, "stock_count": 2,
        "row_ratio": 1.0,
    }]
    assert health["single_source"] is True
    assert health["cross_source_consistency"]["status"] == "unavailable"
    assert health["cross_source_consistency"]["reason"] == "primary_source_unavailable"


def test_primary_symbol_errors_degrade_source_health(monkeypatch):
    _stub_run(
        monkeypatch,
        latest_rows=[],
        fetch=lambda code, start, end, *, session: (
            (_ for _ in ()).throw(RuntimeError("query timeout"))
            if code == "600000"
            else [{
                "code": code, "trading_date": end, "source": "baostock",
            }]
        ),
    )

    result = module.run(asof="2026-08-18", codes=["600000", "000001"])

    health = result["source_health"]
    assert result["status"] == "partial"
    assert health["status"] == "degraded"
    assert health["fallback_used"] is False
    assert health["providers"][0]["status"] == "partial"
    assert health["providers"][0]["failed_stock_count"] == 1
    assert health["providers"][0]["failure_samples"] == ["query timeout"]
    assert health["cross_source_consistency"]["reason"] == "primary_source_fetch_errors"


def test_fallback_with_only_failed_requests_is_source_unavailable(monkeypatch):
    class PrimaryDown:
        def __enter__(self):
            raise RuntimeError("baostock_login_failed:timeout")

        def __exit__(self, *args):
            return None

    class Tdx:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def fetch(self, code, start_date, end_date):
            raise RuntimeError("tdx_query_failed")

    monkeypatch.setattr(module.history, "ensure_schema", lambda: None)
    monkeypatch.setattr(module.history, "cache_stats", lambda: {"row_count": 0})
    monkeypatch.setattr(module.history, "get_latest_daily_bars", lambda codes, asof: [])
    monkeypatch.setattr(module.history, "upsert_daily_bars", lambda rows: len(rows))
    monkeypatch.setattr(module, "BaoStockSession", PrimaryDown)
    monkeypatch.setattr(module, "EasyTdxSession", Tdx)

    result = module.run(asof="2026-08-18", codes=["600000", "000001"])

    health = result["source_health"]
    assert result["status"] == "partial"
    assert health["status"] == "unavailable"
    assert health["degraded"] is False
    assert health["contributions"] == []
    assert health["providers"][1]["status"] == "failed"
    assert health["providers"][1]["failed_stock_count"] == 2
    assert health["cross_source_consistency"] == {
        "status": "unavailable", "sample_size": 0,
        "reason": "no_provider_successful_output",
    }


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
    assert calls["starts"] == ["2025-07-14", "2025-07-14"]  # asof - 400 days
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
    assert module.load_universe() == ["600000", "000001", "000300"]


def test_universe_loader_adds_benchmark_without_masking_missing_universe(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    assert module.load_universe() == []


def test_baostock_symbol_maps_hs300_to_shanghai_index():
    assert module._baostock_symbol("000300") == "sh.000300"
    assert module._baostock_symbol("000001") == "sz.000001"


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


def test_fetch_baostock_uses_index_symbol_mapping(monkeypatch):
    calls = []

    class Result:
        def next(self):
            return False

    session = types.SimpleNamespace(
        bs=types.SimpleNamespace(
            query_history_k_data_plus=lambda *args, **kwargs: calls.append(args[0]) or Result()
        )
    )

    module.BaoStockSession.fetch(session, "000300", "2026-08-18", "2026-08-18")

    assert calls == ["sh.000300"]


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
    def test_a_current_single_day_seed_is_backfilled_backwards(self, monkeypatch):
        """A latest row must not masquerade as 180 days of usable history."""
        starts = []
        coverage_calls = 0

        def coverage(codes, end_date, **kwargs):
            nonlocal coverage_calls
            coverage_calls += 1
            count = 1 if coverage_calls == 1 else 180
            return {
                "600000": {
                    "code": "600000",
                    "bar_count": count,
                    "min_date": "2026-08-19" if count == 1 else "2025-11-01",
                    "max_date": end_date,
                }
            }

        _stub_run(
            monkeypatch,
            latest_rows=[{"code": "600000", "trading_date": "2026-08-19"}],
            fetch=lambda code, start, end, *, session: starts.append(start) or [
                {"code": code, "trading_date": end}
            ],
        )
        monkeypatch.setattr(module.history, "coverage_by_code", coverage)

        result = module.run(asof="2026-08-19", codes=["600000"])

        assert starts == ["2025-07-15"]  # 400 calendar days, safely >180 sessions
        assert result["status"] == "ok"
        assert result["coverage"]["complete"] == 1
        assert result["coverage"]["remaining"] == 0

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
        assert starts["000001"] == "2025-07-15"   # uncached: asof - 400 days

    def test_start_date_is_a_pure_function_of_that_symbols_own_state(self):
        assert module._start_date(None, "2026-08-19") == "2025-07-15"
        assert module._start_date("2026-08-18", "2026-08-19") == "2026-08-19"
        assert module._start_date(
            "2026-08-19", "2026-08-19", bar_count=1
        ) == "2025-07-15"
        assert module._start_date(None, "2026-08-19", full_backfill=True) == "1990-01-01"


class TestCoverageDisclosure:
    def test_under_covered_symbols_are_explicitly_classified(self, monkeypatch):
        monkeypatch.setattr(
            module.history,
            "coverage_by_code",
            lambda codes, end_date, **kwargs: {
                "600000": {
                    "code": "600000", "bar_count": 12,
                    "min_date": "2026-08-01", "max_date": "2026-08-19",
                }
            },
        )
        _stub_run(
            monkeypatch,
            latest_rows=[{"code": "600000", "trading_date": "2026-08-19"}],
            fetch=lambda *args, **kwargs: [],
        )
        monkeypatch.setattr(
            module, "load_universe_metadata",
            lambda: {"600000": {"listed_date": "1999-11-10"}},
        )

        result = module.run(asof="2026-08-19", codes=["600000"])

        assert result["status"] == "partial"
        assert result["coverage"]["remaining"] == 1
        assert result["coverage"]["limited"] == 0
        assert result["coverage"]["classifications"] == {"source_insufficient": 1}

    def test_recent_listing_is_not_reported_as_a_failed_backfill(self, monkeypatch):
        monkeypatch.setattr(
            module.history,
            "coverage_by_code",
            lambda codes, end_date, **kwargs: {
                "688999": {
                    "code": "688999", "bar_count": 12,
                    "min_date": "2026-08-01", "max_date": "2026-08-19",
                }
            },
        )
        _stub_run(
            monkeypatch,
            latest_rows=[{"code": "688999", "trading_date": "2026-08-19"}],
            fetch=lambda *args, **kwargs: [],
        )
        monkeypatch.setattr(
            module, "load_universe_metadata",
            lambda: {"688999": {"listed_date": "2026-08-01"}},
        )

        result = module.run(asof="2026-08-19", codes=["688999"])

        assert result["coverage"]["remaining"] == 0
        assert result["coverage"]["limited"] == 1
        assert result["coverage"]["classifications"] == {"ipo": 1}

    def test_stale_long_listed_symbol_is_classified_as_suspended(self):
        coverage = {
            "600001": {
                "code": "600001", "bar_count": 20,
                "min_date": "2025-07-15", "max_date": "2026-06-01",
            }
        }

        result = module._coverage_result(
            ["600001"], coverage, target_date="2026-08-19",
            attempted={"600001"}, failed_codes=set(), deferred=set(),
            metadata={"600001": {"listed_date": "1998-01-01"}},
        )

        assert result["classifications"] == {"suspended": 1}
        assert result["limited"] == 1
        assert result["remaining"] == 0

    def test_provider_failure_is_distinct_from_insufficient_data(self):
        coverage = {
            "600002": {
                "code": "600002", "bar_count": 1,
                "min_date": "2026-08-18", "max_date": "2026-08-18",
            }
        }

        result = module._coverage_result(
            ["600002"], coverage, target_date="2026-08-19",
            attempted={"600002"}, failed_codes={"600002"}, deferred=set(),
            metadata={"600002": {"listed_date": "1998-01-01"}},
        )

        assert result["classifications"] == {"source_error": 1}
        assert result["remaining"] == 1


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


def test_baostock_login_has_a_hard_socket_timeout_and_restores_default(monkeypatch):
    """BaoStock otherwise blocks forever in recv() when its server goes mute."""
    seen = []

    class Login:
        error_code = "10002001"
        error_msg = "network timeout"

    fake = types.SimpleNamespace(
        login=lambda: seen.append(module.socket.getdefaulttimeout()) or Login(),
        logout=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "baostock", fake)
    before = module.socket.getdefaulttimeout()

    with pytest.raises(RuntimeError, match="baostock_login_failed"):
        with module.BaoStockSession():
            pass

    assert seen == [module.BAOSTOCK_SOCKET_TIMEOUT_SECONDS]
    assert module.socket.getdefaulttimeout() == before
