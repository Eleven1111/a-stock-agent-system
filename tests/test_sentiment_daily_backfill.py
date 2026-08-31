"""sentiment_daily 历史回填 CLI（升级方案 P0-b）。

守：连板高度靠缓存递推而非臆造、回填不到的字段恒 unavailable、缓存为空时
blocked 而不是产出一串空记录。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

import local_market_history as history
import sentiment_daily as sd


ROOT = Path(__file__).resolve().parents[1]


def _backfill_script():
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "sentiment_daily_backfill", ROOT / "scripts" / "sentiment_daily_backfill.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    return tmp_path


def _bar(code, trading_date, close, preclose, *, high=None, open_=None):
    return {
        "code": code, "trading_date": trading_date, "adjust_flag": "qfq",
        "open": preclose if open_ is None else open_,
        "high": close if high is None else high,
        "low": min(close, preclose), "close": close, "preclose": preclose,
        "volume": 1000.0, "amount": 10000.0, "turn": 1.0,
        "pct_chg": (close - preclose) / preclose * 100.0,
        "source": "unit_test", "source_version": "v1",
    }


def _seed_three_limit_up_days():
    """600000 连续三天涨停；600001 一直平盘。"""
    rows = []
    price = 10.0
    for index, day in enumerate(("2026-08-19", "2026-08-20", "2026-08-21")):
        nxt = round(price * 1.1, 2)
        rows.append(_bar("600000", day, nxt, price))
        rows.append(_bar("600001", day, 20.0, 20.0))
        price = nxt
    history.upsert_daily_bars(rows)


def test_backfill_writes_one_record_per_cached_day(state_home):
    _seed_three_limit_up_days()
    result = _backfill_script().backfill(start_date="2026-08-01", end_date="2026-08-31")
    assert result["status"] == "ok"
    assert result["written_days"] == 3
    rows = sd.load_summary()
    assert [row["trading_date"] for row in rows] == [
        "2026-08-19", "2026-08-20", "2026-08-21"
    ]
    assert all(row["limit_count"] == 1 for row in rows)


def test_backfill_recurses_consecutive_board_height(state_home):
    """max_board 由缓存内连续封板天数递推：第三天必须是 3 板，不是 1 板。"""
    _seed_three_limit_up_days()
    _backfill_script().backfill(start_date="2026-08-01", end_date="2026-08-31")
    boards = {row["trading_date"]: row["max_board"] for row in sd.load_summary()}
    assert boards == {"2026-08-19": 1, "2026-08-20": 2, "2026-08-21": 3}


def test_backfill_marks_minute_level_fields_unavailable(state_home):
    """分钟级字段与板块口径字段在日线路径恒不可用，且写进 unavailable_fields。"""
    _seed_three_limit_up_days()
    result = _backfill_script().backfill(start_date="2026-08-01", end_date="2026-08-31")
    assert set(result["always_unavailable_fields"]) == {
        "sector_breadth_top", "leader_damage_intraday_drawdown"
    }
    for row in sd.load_summary():
        assert row["sector_breadth_top"] is None
        assert row["leader_damage_intraday_drawdown"] is None
        assert "sector_breadth_top" in row["unavailable_fields"]


def test_backfill_carries_yesterday_cohort_into_premium(state_home):
    """次日溢价口径：昨日封板股今日的表现，第一天没有昨日梯队 → 不可用。"""
    _seed_three_limit_up_days()
    _backfill_script().backfill(start_date="2026-08-01", end_date="2026-08-31")
    rows = {row["trading_date"]: row for row in sd.load_summary()}
    assert rows["2026-08-19"]["limit_premium_close"] is None
    assert rows["2026-08-20"]["limit_premium_close"] == pytest.approx(10.0, abs=1e-3)
    assert rows["2026-08-20"]["limit_red_ratio"] == pytest.approx(1.0)


def test_backfill_blocks_on_empty_cache(state_home):
    """空缓存必须 blocked，绝不产出一串"全 null"的交易日充数。"""
    history.ensure_schema()
    result = _backfill_script().backfill(start_date="2026-08-01", end_date="2026-08-31")
    assert result["status"] == "blocked"
    assert result["reason"] == "empty_history_cache"
    assert sd.load_summary() == []


def test_backfill_is_idempotent_across_reruns(state_home):
    _seed_three_limit_up_days()
    script = _backfill_script()
    script.backfill(start_date="2026-08-01", end_date="2026-08-31")
    script.backfill(start_date="2026-08-01", end_date="2026-08-31")
    assert sd.summarize()["trading_day_count"] == 3


def test_limit_days_takes_the_most_recent_window(state_home):
    _seed_three_limit_up_days()
    result = _backfill_script().backfill(
        start_date="2026-08-01", end_date="2026-08-31", limit_days=2
    )
    assert result["written_days"] == 2
    assert [row["trading_date"] for row in sd.load_summary()] == [
        "2026-08-20", "2026-08-21"
    ]


def test_halted_symbol_does_not_reset_its_board_height(state_home):
    """停牌日该股不在当日行情里：沿用高度而不是按断板清零。"""
    script = _backfill_script()
    streaks = script.advance_streaks({"600000": 3}, [])
    assert streaks["600000"] == 3
    broken = script.advance_streaks(
        {"600000": 3},
        sd.normalize_rows([{"code": "600000", "prev_close": 10.0, "price": 10.1,
                            "high": 10.2}]),
    )
    assert broken["600000"] == 0


def test_bootstrap_skips_when_minimum_history_already_exists(state_home, monkeypatch):
    script = _backfill_script()
    monkeypatch.setattr(sd, "load_summary", lambda: [
        {"trading_date": f"2026-01-{day:02d}"} for day in range(1, 4)
    ])
    monkeypatch.setattr(script, "backfill", lambda **kwargs: pytest.fail("must not rebuild"))
    result = script.bootstrap(min_days=3, end_date="2026-08-31")
    assert result["status"] == "ok"
    assert result["skipped"] is True


def test_bootstrap_backfills_only_before_existing_forward_rows(state_home, monkeypatch):
    script = _backfill_script()
    monkeypatch.setattr(sd, "load_summary", lambda: [
        {"trading_date": "2026-08-20", "source": "candidate_discovery_inputs"}
    ])
    monkeypatch.setattr(
        history, "trading_dates_between",
        lambda start, end: ["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20"],
    )
    captured = {}

    def fake_backfill(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "written_days": 3}

    monkeypatch.setattr(script, "backfill", fake_backfill)
    result = script.bootstrap(min_days=3, end_date="2026-08-31")
    assert captured == {
        "start_date": "1990-01-01", "end_date": "2026-08-19", "limit_days": 3
    }
    assert result["preserved_forward_days"] == 1


def test_bootstrap_finishes_when_forward_boundary_has_no_earlier_cache(
    state_home, monkeypatch
):
    script = _backfill_script()
    monkeypatch.setattr(sd, "load_summary", lambda: [
        {"trading_date": f"2026-08-{day:02d}"} for day in range(1, 17)
    ])
    monkeypatch.setattr(history, "trading_dates_between", lambda start, end: [])
    monkeypatch.setattr(script, "backfill", lambda **kwargs: pytest.fail("must not overwrite"))

    result = script.bootstrap(min_days=18, end_date="2026-08-31")

    assert result == {
        "status": "ok",
        "skipped": True,
        "reason": "no_history_before_forward_rows",
        "bootstrap_exhausted": True,
        "observed_days": 16,
        "required_days": 18,
        "shortfall_days": 2,
    }
