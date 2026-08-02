"""缠论区间套证据 chan_nested — 日线×60m 同向确定买卖点共现（0 权重展示）。"""

import importlib.util
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
SCRIPTS = PROJ / "skills" / "chanlun-backtest" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cn = _load("chan_nested")


def _sig(idx, bsp_type, is_buy, is_sure=True, date=None):
    return {"idx": idx, "bsp_type": bsp_type, "is_buy": is_buy, "is_sure": is_sure,
            "date": date or f"2026-07-{idx:02d}"}


def test_same_direction_confirmed_signals_produce_record():
    daily = [_sig(58, "1", True, date="2026-07-28")]
    intraday = [_sig(238, "2", True, date="2026-08-01 14:00")]
    records = cn.find_nested_confirmations(daily, 60, intraday, 240)
    assert len(records) == 1
    record = records[0]
    assert record["direction"] == "buy"
    assert record["daily_bsp_type"] == "1" and record["daily_date"] == "2026-07-28"
    assert record["intraday_bsp_type"] == "2" and record["intraday_date"] == "2026-08-01 14:00"


def test_opposite_direction_signals_do_not_pair():
    daily = [_sig(58, "1", True)]
    intraday = [_sig(238, "3a", False)]
    records = cn.find_nested_confirmations(daily, 60, intraday, 240)
    assert records == []


def test_unsure_signal_excluded_from_recent_window():
    daily = [_sig(58, "1", True, is_sure=False)]
    intraday = [_sig(238, "2", True)]
    records = cn.find_nested_confirmations(daily, 60, intraday, 240)
    assert records == []


def test_stale_signal_outside_window_excluded():
    # daily window 默认 10：total_bars=60，idx=40 → 60-40=20 > 10，视为不够新
    daily = [_sig(40, "1", True)]
    intraday = [_sig(238, "2", True)]
    records = cn.find_nested_confirmations(daily, 60, intraday, 240)
    assert records == []


def test_no_signals_returns_empty_list():
    assert cn.find_nested_confirmations([], 60, [], 240) == []


def test_format_nested_notes_renders_zero_weight_display_text():
    records = [{
        "direction": "buy", "daily_bsp_type": "1", "daily_date": "2026-07-28",
        "intraday_bsp_type": "2", "intraday_date": "2026-08-01 14:00",
    }]
    notes = cn.format_nested_notes(records)
    assert len(notes) == 1
    assert "[研究假设]" in notes[0]
    assert "0权重" in notes[0]
    assert "日线bsp1@2026-07-28" in notes[0]
    assert "60m bsp2@2026-08-01 14:00" in notes[0]


def test_pure_function_does_not_mutate_inputs():
    daily = [_sig(58, "1", True)]
    intraday = [_sig(238, "2", True)]
    daily_snapshot = [dict(s) for s in daily]
    intraday_snapshot = [dict(s) for s in intraday]
    cn.find_nested_confirmations(daily, 60, intraday, 240)
    assert daily == daily_snapshot
    assert intraday == intraday_snapshot
