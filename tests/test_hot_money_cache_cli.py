"""Hot-money cache-only / ensure-fresh cron entrypoint tests."""

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "hot-money-tactics" / "scripts" / "analyze.py"
SPEC = importlib.util.spec_from_file_location("hot_money_analyze", SCRIPT)
analyze = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyze)


def _cache_stats(asof="2026-07-03", lianban=2, sectors=1):
    return {
        "input_snapshot": {"ref": "snap"},
        "lianban_count": lianban,
        "sector_count": sectors,
        "ladder_asof": asof,
    }


class _Pool:
    empty = False

    def __len__(self):
        return 2


def test_cache_only_skips_full_market_analysis(monkeypatch, capsys):
    pool = _Pool()
    monkeypatch.setattr(analyze.sys, "argv", ["analyze.py", "20260703", "--cache-only"])
    monkeypatch.setattr(analyze, "get_zt_pool", lambda _asof: pool)
    monkeypatch.setattr(
        analyze,
        "cache_signal_context",
        lambda frame, _asof: _cache_stats() if frame is pool else False,
    )
    monkeypatch.setattr(
        analyze,
        "get_all_stocks",
        lambda: (_ for _ in ()).throw(AssertionError("full analysis should not run")),
    )

    analyze.main()

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready"
    assert result["limitup_total"] == 2
    assert result["ladder_asof"] == "2026-07-03"
    assert result["lianban_count"] == 2
    assert result["sector_count"] == 1


def test_cache_only_returns_nonzero_when_write_fails(monkeypatch, capsys):
    monkeypatch.setattr(analyze.sys, "argv", ["analyze.py", "20260703", "--cache-only"])
    monkeypatch.setattr(analyze, "get_zt_pool", lambda _asof: _Pool())
    monkeypatch.setattr(analyze, "cache_signal_context", lambda _frame, _asof: False)

    with pytest.raises(SystemExit) as exc:
        analyze.main()

    result = json.loads(capsys.readouterr().out)
    assert exc.value.code == 1
    assert result["status"] == "error"


class _EmptyPool:
    empty = True

    def __len__(self):
        return 0


def test_cache_only_empty_pool_on_trading_day_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(analyze.sys, "argv", ["analyze.py", "20260703", "--cache-only"])
    monkeypatch.setattr(analyze, "get_zt_pool", lambda _asof: _EmptyPool())
    monkeypatch.setattr(analyze.a_share_rules, "is_trading_day", lambda _d: True)

    with pytest.raises(SystemExit) as exc:
        analyze.main()

    result = json.loads(capsys.readouterr().out)
    assert exc.value.code == 1
    assert result["status"] == "error"


def test_cache_only_empty_pool_on_non_trading_day_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(analyze.sys, "argv", ["analyze.py", "20260704", "--cache-only"])
    monkeypatch.setattr(analyze, "get_zt_pool", lambda _asof: _EmptyPool())
    monkeypatch.setattr(analyze.a_share_rules, "is_trading_day", lambda _d: False)

    analyze.main()

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "insufficient_data"


def test_ensure_fresh_noop_when_ladder_current(monkeypatch, capsys):
    monkeypatch.setattr(analyze.sys, "argv", ["analyze.py", "--ensure-fresh"])
    monkeypatch.setenv("A_STOCK_TRADING_DATE", "2026-07-06")
    monkeypatch.setattr(
        analyze.state_store, "read_json",
        lambda _path, _default: {"ladder_asof": "2026-07-03"},
    )
    monkeypatch.setattr(
        analyze, "get_zt_pool",
        lambda _asof: (_ for _ in ()).throw(AssertionError("should not fetch when fresh")),
    )

    analyze.ensure_fresh()

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "fresh"
    assert result["ladder_asof"] == "2026-07-03"


def test_ensure_fresh_noop_when_today_ladder_already_written(monkeypatch, capsys):
    """迟到补跑：15:02 当日梯队已写入后才补跑 08:40 backfill → fresh no-op，
    不得回填旧梯队把当日梯队错滚成 prev。"""
    monkeypatch.setenv("A_STOCK_TRADING_DATE", "2026-07-06")
    monkeypatch.setattr(
        analyze.state_store, "read_json",
        lambda _path, _default: {"ladder_asof": "2026-07-06"},
    )
    monkeypatch.setattr(
        analyze, "get_zt_pool",
        lambda _asof: (_ for _ in ()).throw(AssertionError("must not fetch when newer")),
    )
    monkeypatch.setattr(
        analyze, "cache_signal_context",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not write cache")),
    )

    analyze.ensure_fresh()

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "fresh"
    assert result["ladder_asof"] == "2026-07-06"


def test_ensure_fresh_backfills_stale_ladder(monkeypatch, capsys):
    pool = _Pool()
    monkeypatch.setenv("A_STOCK_TRADING_DATE", "2026-07-06")
    monkeypatch.setattr(
        analyze.state_store, "read_json",
        lambda _path, _default: {"ladder_asof": "2026-07-01"},
    )
    monkeypatch.setattr(analyze, "get_zt_pool", lambda _asof: pool)
    monkeypatch.setattr(
        analyze, "cache_signal_context",
        lambda frame, asof: _cache_stats(asof="2026-07-03")
        if frame is pool else False,
    )

    analyze.ensure_fresh()

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "backfilled"
    assert result["ladder_asof"] == "2026-07-03"
    assert result["lianban_count"] == 2


def test_ensure_fresh_backfill_failure_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setenv("A_STOCK_TRADING_DATE", "2026-07-06")
    monkeypatch.setattr(
        analyze.state_store, "read_json",
        lambda _path, _default: {"ladder_asof": "2026-07-01"},
    )
    monkeypatch.setattr(analyze, "get_zt_pool", lambda _asof: _EmptyPool())
    monkeypatch.setattr(analyze.a_share_rules, "is_trading_day", lambda _d: True)

    with pytest.raises(SystemExit) as exc:
        analyze.ensure_fresh()

    result = json.loads(capsys.readouterr().out)
    assert exc.value.code == 1
    assert result["status"] == "error"
