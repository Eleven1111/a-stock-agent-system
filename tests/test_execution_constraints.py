"""P5(a) 成交约束模型 — 金标准合成日用例（一字板 / 炸板回封 / 跌停无量）。

每个用例都是「行为断言 + 样本非空断言」：只断言比例/拒绝而不断言样本非空，
空集会让任何比率恒真（见 rules/container-router.md A 组假绿黑名单）。
"""

import importlib.util
from pathlib import Path

import execution_constraints as xc
import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "skills" / "chanlun-backtest" / "scripts" / "daban_bt_engine.py"
_SPEC = importlib.util.spec_from_file_location("daban_bt_engine", ENGINE)
eng = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(eng)

DATA = ROOT / "skills" / "chanlun-backtest" / "scripts" / "daban_bt_data.py"
_DSPEC = importlib.util.spec_from_file_location("daban_bt_data", DATA)
dat = importlib.util.module_from_spec(_DSPEC)
_DSPEC.loader.exec_module(dat)

CODE = "600255"
ASOF = "2026-06-03"
PREV = 10.0          # 昨收 10.00 → 主板 10cm：涨停 11.00 / 跌停 9.00


def _bar(**kwargs):
    bar = {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 100_000}
    bar.update(kwargs)
    return bar


# --------------------------------------------------------------------------
# 合成日 1：一字涨停全日未开板 → 禁止买入成交
# --------------------------------------------------------------------------
def test_one_word_limit_up_day_refuses_any_buy_fill():
    bar = _bar()                                     # O=H=L=C=11.00 全日封死
    verdict = xc.assess_buy_fill(bar, code=CODE, asof=ASOF, prev_close=PREV)

    assert verdict["limit_state"] == "one_word_limit_up"
    assert verdict["filled"] is False
    assert verdict["fill_ratio"] == 0.0
    assert verdict["reason"] == "one_word_limit_up_no_fill"
    # 样本非空：这一天确实有成交额，拒买不是因为数据缺失。
    assert verdict["day_amount"] and verdict["day_amount"] > 0


def test_one_word_limit_up_event_is_dropped_from_board_overnight_backtest():
    event = _event(t_open=11.0, t_high=11.0, t_low=11.0, t_close=11.0)
    events = [event, _event(code="600256")]          # 第二只是可成交的回封日

    returns = eng.split_returns(events, hold_mode="board_overnight")["h1"]["signal"]

    assert len(eng.filter_universe(events)) == 2      # 样本非空：两只都在 universe
    assert eng.hold_mode_executable(event, "board_overnight") is False
    assert len(returns) == 1                          # 一字那只被剔除，另一只留下


# --------------------------------------------------------------------------
# 合成日 2：炸板回封 → 回封后成交额达阈值才按参与率部分成交
# --------------------------------------------------------------------------
def test_resealed_limit_up_fills_only_up_to_participation_cap():
    # 开在板上 → 炸板到 10.20 → 尾盘回封 11.00；当日成交额 = 100万手×11元×100股/手
    bar = _bar(low=10.2, volume=1_000_000)
    cfg = dict(xc.constraints_config())
    cfg["order_amount"] = 100_000_000.0               # 巨单，必然被参与率截断

    verdict = xc.assess_buy_fill(bar, code=CODE, asof=ASOF, prev_close=PREV, config=cfg)

    assert verdict["limit_state"] == "resealed_limit_up"
    assert verdict["filled"] is True
    assert verdict["reason"] == "partial_fill"
    assert verdict["day_amount"] == pytest.approx(1_100_000_000.0)
    # 参与率 1% → 成交额上限 1100 万，占 1 亿委托的 11%
    assert verdict["fill_amount"] == pytest.approx(11_000_000.0)
    assert verdict["fill_ratio"] == pytest.approx(0.11)
    assert 0.0 < verdict["fill_ratio"] < 1.0


def test_reseal_below_amount_threshold_is_not_tradeable():
    # 回封但全天只成交 1000 手 ≈ 110 万元 < 2000 万门槛 → 板上没换手，买不进
    bar = _bar(low=10.2, volume=1_000)
    verdict = xc.assess_buy_fill(bar, code=CODE, asof=ASOF, prev_close=PREV)

    assert verdict["limit_state"] == "resealed_limit_up"
    assert verdict["filled"] is False
    assert verdict["reason"] == "reseal_amount_below_threshold"
    assert verdict["day_amount"] == pytest.approx(1_100_000.0)   # 样本非空


# --------------------------------------------------------------------------
# 合成日 3：次日跌停无承接量 → 拒卖，顺延至次一可成交时点
# --------------------------------------------------------------------------
def test_limit_down_without_bid_refuses_sell_and_defers():
    bar = {"open": 9.5, "high": 9.5, "low": 9.0, "close": 9.0, "volume": 5_000}
    verdict = xc.assess_sell_fill(bar, code=CODE, asof=ASOF, prev_close=PREV)

    assert verdict["limit_state"] == "limit_down"
    assert verdict["filled"] is False
    assert verdict["defer"] is True
    assert verdict["reason"] == "limit_down_insufficient_bid"
    assert verdict["day_amount"] == pytest.approx(4_500_000.0)   # 样本非空、确实<1000万


def test_limit_down_with_real_bid_volume_can_be_sold():
    bar = {"open": 9.5, "high": 9.5, "low": 9.0, "close": 9.0, "volume": 500_000}
    verdict = xc.assess_sell_fill(bar, code=CODE, asof=ASOF, prev_close=PREV)

    assert verdict["limit_state"] == "limit_down"
    assert verdict["filled"] is True
    assert verdict["defer"] is False


def test_first_sellable_exit_defers_past_a_no_bid_limit_down_session():
    kline = [
        {"date": "2026-06-02", "open": 9.9, "high": 10.0, "low": 9.8,
         "close": 10.0, "volume": 800_000},
        # 买入日（entry_index=1）
        {"date": "2026-06-03", "open": 10.5, "high": 11.0, "low": 10.4,
         "close": 10.8, "volume": 900_000},
        # T+1 跌停无量 → 卖不掉，必须顺延
        {"date": "2026-06-04", "open": 10.2, "high": 10.2, "low": 9.72,
         "close": 9.72, "volume": 3_000},
        {"date": "2026-06-05", "open": 9.9, "high": 10.3, "low": 9.9,
         "close": 10.2, "volume": 700_000},
    ]

    exit_bar, sessions = dat.first_sellable_exit(kline, 1, CODE, "测试")

    assert exit_bar["date"] == "2026-06-05"           # 跳过 06-04 的无量跌停
    assert sessions == 2
    assert exit_bar["volume"] > 0                     # 样本非空


# --------------------------------------------------------------------------
# 滑点分档 + fail-closed
# --------------------------------------------------------------------------
def test_slippage_tiers_split_on_daily_range():
    calm = {"open": 10.0, "high": 10.2, "low": 10.0, "close": 10.1, "volume": 100_000}
    wild = {"open": 10.0, "high": 10.6, "low": 9.8, "close": 10.4, "volume": 100_000}

    assert xc.slippage_bps(calm, code=CODE, asof=ASOF, prev_close=PREV)["tier"] == "normal"
    assert xc.slippage_bps(calm, code=CODE, asof=ASOF, prev_close=PREV)["bps"] == 20.0
    assert xc.slippage_bps(wild, code=CODE, asof=ASOF, prev_close=PREV)["tier"] == "volatile"
    assert xc.slippage_bps(wild, code=CODE, asof=ASOF, prev_close=PREV)["bps"] == 50.0


def test_limit_events_use_constraint_model_not_fixed_slippage():
    tier = xc.slippage_bps(_bar(), code=CODE, asof=ASOF, prev_close=PREV)

    assert tier["tier"] == "limit_event"
    assert tier["bps"] is None
    assert tier["reason"] == "constraint_model_applies"


@pytest.mark.parametrize("bar,prev", [
    ({"open": None, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1}, PREV),
    ({"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": None}, PREV),
    ({"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1}, None),
])
def test_missing_data_fails_closed_on_both_sides(bar, prev):
    buy = xc.assess_buy_fill(bar, code=CODE, asof=ASOF, prev_close=prev)
    sell = xc.assess_sell_fill(bar, code=CODE, asof=ASOF, prev_close=prev)

    assert buy["filled"] is False and buy["fill_ratio"] == 0.0
    assert sell["filled"] is False and sell["defer"] is True


def test_v2_event_without_t_day_bar_fails_closed_in_engine():
    stale = _event()
    for field in ("t_open", "t_high", "t_low", "t_prev_close"):
        stale.pop(field)

    verdict = eng.board_entry_fill(stale)

    assert verdict["filled"] is False
    assert verdict["reason"] == "missing_t_day_bar_fail_closed"
    assert eng.hold_mode_executable(stale, "board_overnight") is False


def test_slippage_tiering_reprices_only_when_opted_in():
    event = _event(t_open=11.0, t_high=11.0, t_low=11.0, t_close=11.0)
    event.update({"t1_open": 10.5, "t1_high": 10.7, "t1_low": 10.45,
                  "t1_close": 10.6, "t_close": 11.0})

    default = eng._event_return(event, "open_close", eng.DEFAULT_COST)
    tiered = eng._event_return(event, "open_close", eng.DEFAULT_COST,
                               slippage_tiering=True)

    assert eng.event_slippage_bps(event, "open_close") == 20.0   # 常态档
    assert tiered == pytest.approx(default)   # 20bp == 既有固定 slippage 0.002


def _event(code=CODE, **kwargs):
    """回封涨停的 T 日事件（默认可成交），字段齐备到 event_table v3。"""
    event = {
        "code": code, "name": "X", "date": ASOF, "entry_date": "2026-06-04",
        "t_prev_close": PREV, "t_open": 11.0, "t_high": 11.0, "t_low": 10.2,
        "t_close": 11.0, "t_volume": 1_000_000, "t_amount": None,
        "t1_open": 11.2, "t1_high": 11.5, "t1_low": 11.0, "t1_close": 11.3,
        "t1_volume": 800_000, "t1_amount": None,
        "exit_date": "2026-06-05", "exit_close": 11.4, "holding_sessions": 1,
        "first_seal": "092500", "is_st": False,
    }
    event.update(kwargs)
    return event
