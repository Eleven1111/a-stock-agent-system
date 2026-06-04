"""打板回测引擎层 — 合成事件表单测（期望值手推）"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts" / "daban_bt_engine.py"
SPEC = importlib.util.spec_from_file_location("daban_bt_engine", SCRIPT)
eng = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eng)


def _ev(code="600255", t_close=10.0, t1_open=10.2, t1_close=11.0,
        first_seal="092500", date="2026-06-03", is_st=False, name="X"):
    return {"code": code, "name": name, "t_close": t_close, "t1_open": t1_open,
            "t1_close": t1_close, "first_seal": first_seal, "date": date, "is_st": is_st}


def test_net_return_with_costs():
    # buy=10 sell=11：eff_buy=10*1.00225, eff_sell=11*0.99725 → 0.0945123
    r = eng.net_return(10.0, 11.0)
    assert r == pytest.approx(0.0945123, abs=1e-6)


def test_net_return_rejects_bad_price():
    with pytest.raises(ValueError):
        eng.net_return(0.0, 11.0)


def test_parse_seal_minutes():
    assert eng.parse_seal_minutes("092500") == 565
    assert eng.parse_seal_minutes("09:25") == 565
    assert eng.parse_seal_minutes("093218") == 572
    assert eng.parse_seal_minutes("") is None
    assert eng.parse_seal_minutes("bad") is None


def test_universe_excludes_non_mainboard_st_and_missing():
    assert eng.passes_universe(_ev(code="600255")) is True
    assert eng.passes_universe(_ev(code="300123")) is False   # 创业板 20cm
    assert eng.passes_universe(_ev(code="688001")) is False   # 科创板
    assert eng.passes_universe(_ev(is_st=True)) is False
    assert eng.passes_universe(_ev(name="ST康美")) is False    # 名称含 ST → 5cm
    assert eng.passes_universe(_ev(t1_open=None)) is False


def test_gap_and_h1_signal_window():
    assert eng.gap_pct(_ev(t_close=10.0, t1_open=10.2)) == pytest.approx(2.0)
    assert eng.is_h1_signal(_ev(t1_open=10.2)) is True        # gap=2 in [-1,3]
    assert eng.is_h1_signal(_ev(t1_open=10.5)) is False       # gap=5 out
    assert eng.is_h1_signal(_ev(t1_open=9.85)) is False       # gap=-1.5 out


def test_auction_vs_intraday_seal():
    assert eng.is_auction_seal(_ev(first_seal="092500")) is True
    assert eng.is_auction_seal(_ev(first_seal="093000")) is False
    assert eng.is_intraday_seal(_ev(first_seal="100000")) is True


def test_split_by_date():
    evs = [_ev(date="2026-04-01"), _ev(date="2026-05-15"), _ev(date="2026-05-20")]
    is_set, oos_set = eng.split_by_date(evs, "2026-05-16")
    assert len(is_set) == 2 and len(oos_set) == 1


def test_hold_mode_board_overnight_uses_t_close():
    ev = _ev(t_close=10.0, t1_open=10.5, t1_close=11.0)
    oc = eng._event_return(ev, "open_close", eng.DEFAULT_COST)      # 买10.5卖11.0
    bo = eng._event_return(ev, "board_overnight", eng.DEFAULT_COST)  # 买10.0卖11.0
    assert bo > oc                                  # 含隔夜跳空 → 收益更高
    assert bo == pytest.approx(eng.net_return(10.0, 11.0))


def test_split_returns_hold_mode_threads_through():
    events = [_ev(code="600255", t_close=10.0, t1_open=10.5, t1_close=11.0)]
    bo = eng.split_returns(events, hold_mode="board_overnight")["h1"]["control"][0]
    oc = eng.split_returns(events, hold_mode="open_close")["h1"]["control"][0]
    assert bo == pytest.approx(eng.net_return(10.0, 11.0))
    assert oc == pytest.approx(eng.net_return(10.5, 11.0))


def test_split_returns_structure_and_filtering():
    events = [
        _ev(code="600255", t1_open=10.2, first_seal="092500"),   # h1 signal + auction
        _ev(code="600256", t1_open=10.5, first_seal="100000"),   # not h1 + intraday
        _ev(code="300999", t1_open=10.2, first_seal="092500"),   # excluded by universe
    ]
    out = eng.split_returns(events)
    assert len(out["h1"]["control"]) == 2     # 两只主板票
    assert len(out["h1"]["signal"]) == 1      # 仅 gap=2 那只
    assert len(out["h2"]["auction"]) == 1
    assert len(out["h2"]["intraday"]) == 1
