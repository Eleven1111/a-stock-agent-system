"""Smoke test for the chan.py offline reference driver.

Verifies the driver can run chan.py's structure analysis fully offline
on a synthetic K-line series with obvious swing highs/lows, and that
it returns non-empty, well-formed 笔 (bi) and 买卖点 (bsp) records.
This is the oracle used for future differential testing against the
production chanlun rewrite (docs/chanlun-upgrade-plan-2026-08.md).
"""

import datetime
import os
import sys

import pytest

PROJ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# chan.py 参考实现（差分 oracle）使用 typing.Self，要求 Python 3.11+；
# 生产侧 chan_* 模块不依赖它，仍支持 3.10。低版本上跳过差分校验而非 collection 报错。
if sys.version_info < (3, 11):  # pragma: no cover - 版本相关分支
    pytest.skip("chan.py 参考实现需要 Python 3.11+（typing.Self）", allow_module_level=True)

REFERENCE_ROOT = os.path.join(PROJ, "third_party", "chan_py_reference")
if REFERENCE_ROOT not in sys.path:
    sys.path.insert(0, REFERENCE_ROOT)

from offline_driver import BiRecord, BspRecord, SyntheticBar, run_offline  # noqa: E402


def _zigzag_bars(swing_points, bars_per_leg=6):
    """Build a synthetic daily OHLC series that walks linearly between
    the given price levels, producing unambiguous swing highs/lows
    (each leg strictly monotonic, so fenxing/bi detection is unambiguous).
    """
    bars = []
    start_date = datetime.date(2024, 1, 2)
    day_idx = 0
    price = swing_points[0]
    for leg_end in swing_points[1:]:
        step = (leg_end - price) / bars_per_leg
        for _ in range(bars_per_leg):
            open_px = price
            close_px = price + step
            high_px = max(open_px, close_px) + abs(step) * 0.2
            low_px = min(open_px, close_px) - abs(step) * 0.2
            bar_date = start_date + datetime.timedelta(days=day_idx)
            bars.append(
                SyntheticBar(
                    date=bar_date.isoformat(),
                    open=round(open_px, 2),
                    high=round(high_px, 2),
                    low=round(low_px, 2),
                    close=round(close_px, 2),
                )
            )
            price = close_px
            day_idx += 1
    return bars


def _sample_bars():
    # Ten legs of alternating up/down swings with clear tops/bottoms,
    # overlapping enough to form a zhongshu (central pivot) followed by
    # a breakout: 10 -> 14 -> 11 -> 15 -> 12 -> 16 -> 13 -> 17 -> 14 -> 22 -> 20
    # (60 bars). Verified offline to produce both a non-trivial bi list
    # and at least one buy/sell point.
    return _zigzag_bars(
        [10.0, 14.0, 11.0, 15.0, 12.0, 16.0, 13.0, 17.0, 14.0, 22.0, 20.0],
        bars_per_leg=6,
    )


def test_sample_bars_has_at_least_sixty_bars():
    bars = _sample_bars()
    assert len(bars) >= 60


def test_run_offline_produces_bi_list_without_network():
    bars = _sample_bars()
    bi_records, _bsp_records = run_offline(bars)

    assert len(bi_records) > 0
    for bi in bi_records:
        assert isinstance(bi, BiRecord)
        assert bi.dir in ("UP", "DOWN")
        assert bi.begin
        assert bi.end
        assert isinstance(bi.is_sure, bool)


def test_run_offline_bsp_records_have_complete_fields():
    bars = _sample_bars()
    _bi_records, bsp_records = run_offline(bars)

    # Not every synthetic series is guaranteed to trigger a buy/sell
    # point, but this ten-leg zigzag (overlapping oscillation forming a
    # zhongshu, then a breakout leg) was verified offline to produce
    # one. If this ever regresses to empty, the swing pattern above
    # needs to be revisited, not this assertion.
    assert len(bsp_records) > 0
    for bsp in bsp_records:
        assert isinstance(bsp, BspRecord)
        assert isinstance(bsp.bi_idx, int)
        assert isinstance(bsp.is_buy, bool)
        assert isinstance(bsp.types, tuple)
        assert len(bsp.types) > 0
        assert bsp.time


def test_run_offline_rejects_too_few_bars():
    try:
        run_offline([SyntheticBar(date="2024-01-02", open=1, high=1, low=1, close=1)])
        assert False, "expected ValueError for insufficient bars"
    except ValueError:
        pass
