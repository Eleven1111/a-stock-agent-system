"""Hot-money cache-only cron entrypoint tests."""

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "hot-money-tactics" / "scripts" / "analyze.py"
SPEC = importlib.util.spec_from_file_location("hot_money_analyze", SCRIPT)
analyze = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyze)


class _Pool:
    empty = False

    def __len__(self):
        return 2


def test_cache_only_skips_full_market_analysis(monkeypatch, capsys):
    pool = _Pool()
    monkeypatch.setattr(analyze.sys, "argv", ["analyze.py", "--cache-only"])
    monkeypatch.setattr(analyze, "get_zt_pool", lambda _asof: pool)
    monkeypatch.setattr(
        analyze,
        "cache_signal_context",
        lambda frame, _asof: frame is pool,
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


def test_cache_only_returns_nonzero_when_write_fails(monkeypatch, capsys):
    monkeypatch.setattr(analyze.sys, "argv", ["analyze.py", "--cache-only"])
    monkeypatch.setattr(analyze, "get_zt_pool", lambda _asof: _Pool())
    monkeypatch.setattr(analyze, "cache_signal_context", lambda _frame, _asof: False)

    with pytest.raises(SystemExit) as exc:
        analyze.main()

    result = json.loads(capsys.readouterr().out)
    assert exc.value.code == 1
    assert result["status"] == "error"
